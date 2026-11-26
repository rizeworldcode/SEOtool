"""
LinkPilot AI — Phase 1B Milestone 5: Domain Authority (LP-DA) Engine Test Suite.
Contains 40 isolated unit, repository, transaction, pipeline, and CLI tests (T5-01 to T5-40).
All tests execute strictly against in-memory SQLite (sqlite:///:memory:) or isolated tmp_path
files, guaranteeing zero side-effects on the physical production database.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from app.authority.domain_scorer import (
    DA_ALGORITHM_VERSION,
    DEFAULT_DA_ALPHA,
    DEFAULT_DA_BETA,
    DEFAULT_DA_EPSILON,
    DEFAULT_DA_GAMMA,
    DEFAULT_DA_REF_N,
    DEFAULT_DA_TOP_K,
    aggregate_domain_authority_from_page_records,
    canonicalize_domain_lookup,
    compute_domain_breadth_score,
    compute_regularized_harmonic_top_k,
    compute_single_domain_authority,
    reconstruct_lp_da_level1,
    validate_da_parameters,
)
from app.authority.graph import GraphVertex, PhysicalEdgeOccurrence, PhysicalGraph, VertexType
from app.authority.scorer import (
    AuthorityScoringConfig,
    PageRankOrchestrator,
    compute_and_persist_authority_scores,
)
from app.commands.cli import app as cli_app
from app.database.models import (
    Base,
    CrawlRun,
    DataSource,
    Domain,
    DomainAuthorityScore,
    LinkOccurrence,
    Page,
    PageAuthorityScore,
    PageLink,
)
from app.database.repositories import CrawlRepository


# ==============================================================================
# ISOLATED IN-MEMORY FIXTURES
# ==============================================================================

@pytest.fixture
def mem_session() -> Session:
    """
    Create a completely isolated in-memory SQLite session with all 18 tables.
    Configures SQLAlchemy's documented pysqlite SAVEPOINT listener so nested
    SAVEPOINTs and outer transaction rollbacks behave per standard SQL semantics.
    """
    from sqlalchemy import event

    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _do_connect(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _do_begin(conn):
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Sess()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _build_synthetic_multi_domain_graph() -> PhysicalGraph:
    """Construct an in-memory multi-domain directed graph for pipeline testing."""
    graph = PhysicalGraph()
    nodes = [
        (1, "https://alpha.com/", "alpha.com"),
        (2, "https://alpha.com/guide", "alpha.com"),
        (3, "https://alpha.com/faq", "alpha.com"),
        (4, "https://beta.org/", "beta.org"),
        (5, "https://beta.org/docs", "beta.org"),
    ]
    for pid, url, dom in nodes:
        graph.add_vertex(
            GraphVertex(
                key=url,
                raw_url=url,
                normalized_url=url,
                domain=dom,
                site_boundary=dom,
                vertex_type=VertexType.INTERNAL_CRAWLED,
                is_authoritative_scored_page=True,
                page_id=pid,
                http_status=200,
            )
        )

    edges = [
        ("https://alpha.com/guide", "https://alpha.com/"),
        ("https://alpha.com/faq", "https://alpha.com/"),
        ("https://alpha.com/", "https://alpha.com/guide"),
        ("https://beta.org/docs", "https://beta.org/"),
        ("https://beta.org/", "https://alpha.com/"),
    ]
    for src, tgt in edges:
        graph.add_edge_occurrence(PhysicalEdgeOccurrence(src, tgt, tgt))
    graph.finalize()
    return graph


# ==============================================================================
# CATEGORY 1: MATHEMATICAL AGGREGATION TESTS (T5-01 to T5-14)
# ==============================================================================

def test_t5_01_empty_domain_produces_zero_da():
    """T5-01: An empty domain (N=0) produces 0.0 for all components and LP-DA."""
    res = compute_single_domain_authority("empty.com", [])
    assert res.scored_pages_count == 0
    assert res.anchor_page_pa == 0.0
    assert res.top_k_harmonic_pa == 0.0
    assert res.breadth_score == 0.0
    assert res.mean_page_pa == 0.0
    assert res.lp_da_score == 0.0
    assert res.lp_da_display == 0.0


def test_t5_02_single_page_domain_anchor_equality():
    """T5-02: Single positive page (N=1) has A = T_1 = M = s_1 and B(1) = log10(2)/log10(101)."""
    res = compute_single_domain_authority("single.com", [0.75])
    assert res.scored_pages_count == 1
    assert math.isclose(res.anchor_page_pa, 0.75, rel_tol=1e-12)
    assert math.isclose(res.top_k_harmonic_pa, 0.75, rel_tol=1e-9)
    assert math.isclose(res.mean_page_pa, 0.75, rel_tol=1e-12)
    expected_b = math.log10(2.0) / math.log10(101.0)
    assert math.isclose(res.breadth_score, expected_b, rel_tol=1e-12)
    expected_da = 0.40 * 0.75 + 0.35 * res.top_k_harmonic_pa + 0.25 * expected_b * 0.75
    assert math.isclose(res.lp_da_score, expected_da, rel_tol=1e-12)
    assert res.lp_da_display == round(expected_da * 100.0, 2)


def test_t5_03_domain_with_fewer_than_k_pages():
    """T5-03: Domain with N=3 < k=5 evaluates harmonic mean over m=3 pages."""
    scores = [0.80, 0.60, 0.40]
    res = compute_single_domain_authority("three.com", scores, top_k=5)
    assert res.scored_pages_count == 3
    assert res.configured_top_k == 5
    assert math.isclose(res.anchor_page_pa, 0.80, rel_tol=1e-12)
    assert math.isclose(res.top_k_harmonic_pa, 0.553854, abs_tol=1e-6)
    assert math.isclose(res.lp_da_score, 0.558906, abs_tol=1e-6)


def test_t5_04_domain_with_exactly_k_pages():
    """T5-04: Domain with N=5 == k=5 evaluates all 5 pages."""
    scores = [0.90, 0.80, 0.70, 0.60, 0.50]
    res = compute_single_domain_authority("five.com", scores, top_k=5)
    assert res.scored_pages_count == 5
    assert math.isclose(res.anchor_page_pa, 0.90, rel_tol=1e-12)
    assert math.isclose(res.top_k_harmonic_pa, 0.670574, abs_tol=1e-6)
    assert math.isclose(res.mean_page_pa, 0.70, rel_tol=1e-12)
    assert math.isclose(res.lp_da_score, 0.662642, abs_tol=1e-6)


def test_t5_05_domain_with_more_than_k_pages():
    """T5-05: Domain with N=8 > k=5 truncates T_k to the 5 highest scores while M and B use all 8."""
    scores = [0.90, 0.80, 0.70, 0.60, 0.50, 0.30, 0.20, 0.10]
    res5 = compute_single_domain_authority("five.com", scores[:5], top_k=5)
    res8 = compute_single_domain_authority("eight.com", scores, top_k=5)
    assert res8.scored_pages_count == 8
    assert math.isclose(res8.top_k_harmonic_pa, res5.top_k_harmonic_pa, rel_tol=1e-12)
    assert res8.breadth_score > res5.breadth_score
    assert math.isclose(res8.mean_page_pa, sum(scores) / 8.0, rel_tol=1e-12)
    assert math.isclose(res8.lp_da_score, 0.655700, abs_tol=1e-6)


def test_t5_06_all_zero_scores_returns_zero():
    """T5-06: Domain where all pages have PA = 0.0 produces T_k = 0.0 and LP-DA = 0.0."""
    res = compute_single_domain_authority("zeros.com", [0.0, 0.0, 0.0, 0.0])
    assert res.scored_pages_count == 4
    assert res.anchor_page_pa == 0.0
    assert res.top_k_harmonic_pa == 0.0
    assert res.mean_page_pa == 0.0
    assert res.breadth_score > 0.0
    assert res.lp_da_score == 0.0


def test_t5_07_mixed_zero_and_positive_scores():
    """T5-07: Mixed zero and positive scores in top-k remain continuous without ZeroDivisionError."""
    res = compute_single_domain_authority("mixed.com", [0.80, 0.60, 0.0, 0.0])
    assert math.isclose(res.top_k_harmonic_pa, 0.000100, abs_tol=1e-6)
    assert math.isclose(res.lp_da_score, 0.350549, abs_tol=1e-6)


def test_t5_08_small_positive_scores_harmonic_accuracy():
    """T5-08: Small positive uniform scores [1e-6, 1e-6, 1e-6] preserve exact harmonic value 1e-6."""
    t_val = compute_regularized_harmonic_top_k([1e-6, 1e-6, 1e-6], top_k=5, epsilon=1e-4)
    assert math.isclose(t_val, 1e-6, rel_tol=1e-9, abs_tol=1e-12)


def test_t5_09_breadth_scaling_monotonicity():
    """T5-09: Breadth score B(N) is strictly increasing for N in 1..100."""
    prev = compute_domain_breadth_score(0)
    assert prev == 0.0
    for n in range(1, 101):
        curr = compute_domain_breadth_score(n, reference_page_count=100.0)
        assert curr > prev, f"Breadth failed strict monotonicity at N={n}"
        prev = curr


def test_t5_10_breadth_saturation_at_reference():
    """T5-10: Breadth score saturates at 1.0 for N >= N_ref (100)."""
    assert math.isclose(compute_domain_breadth_score(100, 100.0), 1.0, rel_tol=1e-12)
    assert compute_domain_breadth_score(101, 100.0) == 1.0
    assert compute_domain_breadth_score(10000, 100.0) == 1.0


def test_t5_11_outlier_resistance_evaluation():
    """T5-11: A domain with 5 strong pillar pages outscores an outlier domain with 1 strong page and 4 weak pages."""
    outlier = compute_single_domain_authority("outlier.com", [0.90, 0.01, 0.01, 0.01, 0.01])
    balanced = compute_single_domain_authority("balanced.com", [0.75, 0.72, 0.70, 0.68, 0.65])
    assert balanced.top_k_harmonic_pa > outlier.top_k_harmonic_pa * 5.0
    assert balanced.lp_da_score > outlier.lp_da_score


def test_t5_12_score_bounds_fuzzing():
    """T5-12: 1,000 randomized domain vectors all produce LP-DA in [0.0, 1.0]."""
    rng = random.Random(20261119)
    for _ in range(1000):
        n = rng.randint(0, 150)
        vec = [rng.random() for _ in range(n)]
        res = compute_single_domain_authority("fuzz.com", vec)
        assert 0.0 <= res.lp_da_score <= 1.0
        assert 0.0 <= res.lp_da_display <= 100.0


def test_t5_13_coefficient_convexity_and_invalid_input_validation():
    """T5-13: Validates weight convexity sum=1.0 (with math.isclose) and rejects NaN, inf, <0, >1."""
    # Valid IEEE 754 triplet 0.1 + 0.2 + 0.7
    validate_da_parameters(alpha=0.1, beta=0.2, gamma=0.7)

    with pytest.raises(ValueError):
        validate_da_parameters(alpha=0.5, beta=0.3, gamma=0.3)
    with pytest.raises(ValueError):
        validate_da_parameters(alpha=-0.1, beta=0.6, gamma=0.5)
    with pytest.raises(ValueError):
        AuthorityScoringConfig(da_alpha=0.5, da_beta=0.5, da_gamma=0.5)

    for bad_score in [float("nan"), float("inf"), float("-inf"), -0.01, 1.01, True, None]:
        with pytest.raises(ValueError):
            compute_single_domain_authority("bad.com", [bad_score])  # type: ignore[list-item]


def test_t5_14_deterministic_tie_breaking():
    """T5-14: Tie-breaking sorts by (-lp_pa_score, -raw_pagerank, normalized_url ASC)."""
    records = [
        {"domain": "tie.com", "normalized_url": "https://tie.com/c", "lp_pa_score": 0.50, "raw_pagerank": 0.10, "is_internal_page": True},
        {"domain": "tie.com", "normalized_url": "https://tie.com/b", "lp_pa_score": 0.50, "raw_pagerank": 0.20, "is_internal_page": True},
        {"domain": "tie.com", "normalized_url": "https://tie.com/a", "lp_pa_score": 0.50, "raw_pagerank": 0.20, "is_internal_page": True},
    ]
    res_a = aggregate_domain_authority_from_page_records(records, top_k=2)
    res_b = aggregate_domain_authority_from_page_records(list(reversed(records)), top_k=2)
    assert res_a[0].top_page_urls == ["https://tie.com/a", "https://tie.com/b"]
    assert res_a[0].top_page_urls == res_b[0].top_page_urls
    assert res_a[0].lp_da_score == res_b[0].lp_da_score


# ==============================================================================
# CATEGORY 2: REPOSITORY OPERATIONS TESTS (T5-15 to T5-21)
# ==============================================================================

def test_t5_15_repo_upsert_domain_authority_scores_success(mem_session: Session):
    """T5-15: Upsert inserts new DomainAuthorityScore records accurately."""
    repo = CrawlRepository(mem_session)
    comp = compute_single_domain_authority("alpha.com", [0.80, 0.60])
    count = repo.upsert_domain_authority_scores([comp.to_record_dict()])
    mem_session.commit()

    assert count == 1
    stored = repo.get_domain_authority_score("alpha.com")
    assert stored is not None
    assert stored.domain_name == "alpha.com"
    assert math.isclose(stored.lp_da_score, comp.lp_da_score, rel_tol=1e-12)
    assert stored.scored_pages_count == 2


def test_t5_16_repo_upsert_idempotent_replacement(mem_session: Session):
    """T5-16: Re-running upsert replaces existing domain records without duplicate errors."""
    repo = CrawlRepository(mem_session)
    c1 = compute_single_domain_authority("alpha.com", [0.40]).to_record_dict()
    repo.upsert_domain_authority_scores([c1])
    mem_session.commit()

    c2 = compute_single_domain_authority("alpha.com", [0.90, 0.85]).to_record_dict()
    repo.upsert_domain_authority_scores([c2])
    mem_session.commit()

    all_rows = mem_session.query(DomainAuthorityScore).all()
    assert len(all_rows) == 1
    assert all_rows[0].scored_pages_count == 2
    assert math.isclose(all_rows[0].anchor_page_pa, 0.90, rel_tol=1e-12)


def test_t5_17_repo_upsert_target_domain_scope(mem_session: Session):
    """T5-17: Target-domain scoped upsert replaces only the target domain and preserves others."""
    repo = CrawlRepository(mem_session)
    c_alpha = compute_single_domain_authority("alpha.com", [0.50]).to_record_dict()
    c_beta = compute_single_domain_authority("beta.org", [0.70]).to_record_dict()
    repo.upsert_domain_authority_scores([c_alpha, c_beta])
    mem_session.commit()

    c_alpha_new = compute_single_domain_authority("alpha.com", [0.95]).to_record_dict()
    repo.upsert_domain_authority_scores([c_alpha_new], target_domain="alpha.com")
    mem_session.commit()

    alpha_row = repo.get_domain_authority_score("alpha.com")
    beta_row = repo.get_domain_authority_score("beta.org")
    assert alpha_row is not None and math.isclose(alpha_row.anchor_page_pa, 0.95)
    assert beta_row is not None and math.isclose(beta_row.anchor_page_pa, 0.70)


def test_t5_18_repo_get_domain_authority_score(mem_session: Session):
    """T5-18: Retrieve persisted DomainAuthorityScore by exact domain_name."""
    repo = CrawlRepository(mem_session)
    comp = compute_single_domain_authority("zoniraz.com", [0.88, 0.72])
    repo.upsert_domain_authority_scores([comp.to_record_dict()])
    mem_session.commit()

    found = repo.get_domain_authority_score("zoniraz.com")
    assert found is not None
    assert found.domain_name == "zoniraz.com"
    assert found.algorithm_version == DA_ALGORITHM_VERSION


def test_t5_19_repo_get_missing_domain_returns_none(mem_session: Session):
    """T5-19: Querying a non-existent domain returns None."""
    repo = CrawlRepository(mem_session)
    assert repo.get_domain_authority_score("nonexistent.example.com") is None


def test_t5_20_repo_get_top_domain_authority_scores(mem_session: Session):
    """T5-20: Top domain leaderboard orders by lp_da_score DESC, then domain_name ASC."""
    repo = CrawlRepository(mem_session)
    d1 = compute_single_domain_authority("low.com", [0.20]).to_record_dict()
    d2 = compute_single_domain_authority("high.com", [0.95, 0.90]).to_record_dict()
    d3 = compute_single_domain_authority("mid.com", [0.55]).to_record_dict()
    repo.upsert_domain_authority_scores([d1, d2, d3])
    mem_session.commit()

    top = repo.get_top_domain_authority_scores(limit=2)
    assert [d.domain_name for d in top] == ["high.com", "mid.com"]


def test_t5_21_repo_deterministic_fk_and_nullable_handling(mem_session: Session):
    """T5-21: Resolves domain_id deterministically (domain_name match > registered_domain, id ASC) or None."""
    dom_sub = Domain(id=10, domain_name="blog.alpha.com", registered_domain="alpha.com", tld="com")
    dom_exact = Domain(id=20, domain_name="alpha.com", registered_domain="alpha.com", tld="com")
    mem_session.add_all([dom_sub, dom_exact])
    mem_session.commit()

    repo = CrawlRepository(mem_session)
    # Exact domain_name match (id=20) takes priority over registered_domain match (id=10)
    assert repo.resolve_domain_id("alpha.com") == 20
    # Unregistered domain resolves to None
    assert repo.resolve_domain_id("unregistered.org") is None

    c_alpha = compute_single_domain_authority("alpha.com", [0.80]).to_record_dict()
    c_unreg = compute_single_domain_authority("unregistered.org", [0.60]).to_record_dict()
    repo.upsert_domain_authority_scores([c_alpha, c_unreg])
    mem_session.commit()

    assert repo.get_domain_authority_score("alpha.com").domain_id == 20
    assert repo.get_domain_authority_score("unregistered.org").domain_id is None


# ==============================================================================
# CATEGORY 3: TRANSACTION & SAVEPOINT HARDENING TESTS (T5-22 to T5-26, T5-39)
# ==============================================================================

def test_t5_22_savepoint_isolation_caller_session(mem_session: Session, monkeypatch):
    """T5-22: Orchestrator uses session.begin_nested() SAVEPOINT when a caller session is provided."""
    nested_called = {"count": 0}
    orig_begin_nested = mem_session.begin_nested

    def tracked_begin_nested():
        nested_called["count"] += 1
        return orig_begin_nested()

    monkeypatch.setattr(mem_session, "begin_nested", tracked_begin_nested)
    graph = _build_synthetic_multi_domain_graph()
    orchestrator = PageRankOrchestrator(AuthorityScoringConfig(compute_da=True))
    res = orchestrator.run(graph=graph, session=mem_session)

    assert res.status == "COMPLETED"
    assert nested_called["count"] == 1


def test_t5_23_caller_session_neither_committed_nor_closed(mem_session: Session):
    """T5-23: Orchestrator never commits or closes a caller-supplied session."""
    graph = _build_synthetic_multi_domain_graph()
    orchestrator = PageRankOrchestrator(AuthorityScoringConfig(compute_da=True))
    res = orchestrator.run(graph=graph, session=mem_session)
    assert res.status == "COMPLETED"
    assert mem_session.in_transaction()
    assert mem_session.is_active

    # Rolling back caller's outer transaction undoes both PA and DA writes
    mem_session.rollback()
    assert mem_session.query(PageAuthorityScore).count() == 0
    assert mem_session.query(DomainAuthorityScore).count() == 0


def test_t5_24_dual_persistence_atomic_rollback_on_da_failure(mem_session: Session, monkeypatch):
    """T5-24: If DA persistence fails inside the SAVEPOINT, preceding PA writes roll back atomically."""
    graph = _build_synthetic_multi_domain_graph()
    orchestrator = PageRankOrchestrator(AuthorityScoringConfig(compute_da=True))

    def failing_upsert_da(self, records, target_domain=None):
        raise IntegrityError("Simulated DA constraint failure", params=None, orig=Exception("boom"))

    monkeypatch.setattr(CrawlRepository, "upsert_domain_authority_scores", failing_upsert_da)
    res = orchestrator.run(graph=graph, session=mem_session)

    assert res.status == "FAILED"
    assert "Persistence transaction failed" in (res.error_message or "")
    # Preceding PA writes must have been rolled back by the SAVEPOINT!
    assert mem_session.query(PageAuthorityScore).count() == 0
    assert mem_session.query(DomainAuthorityScore).count() == 0


def test_t5_25_dual_persistence_atomic_rollback_on_pa_failure(mem_session: Session, monkeypatch):
    """T5-25: If PA persistence fails, DA persistence is never executed and transaction rolls back."""
    graph = _build_synthetic_multi_domain_graph()
    orchestrator = PageRankOrchestrator(AuthorityScoringConfig(compute_da=True))
    da_called = {"called": False}

    def failing_upsert_pa(self, records, target_domain=None):
        raise OperationalError("Simulated PA write failure", params=None, orig=Exception("pa_fail"))

    def spy_upsert_da(self, records, target_domain=None):
        da_called["called"] = True
        return 0

    monkeypatch.setattr(CrawlRepository, "upsert_page_authority_scores", failing_upsert_pa)
    monkeypatch.setattr(CrawlRepository, "upsert_domain_authority_scores", spy_upsert_da)
    res = orchestrator.run(graph=graph, session=mem_session)

    assert res.status == "FAILED"
    assert da_called["called"] is False
    assert mem_session.query(DomainAuthorityScore).count() == 0


def test_t5_26_previous_committed_scores_preserved_on_failure(mem_session: Session, monkeypatch):
    """T5-26: Previously committed PA and DA scores remain intact when a subsequent run fails."""
    graph = _build_synthetic_multi_domain_graph()
    orchestrator = PageRankOrchestrator(AuthorityScoringConfig(compute_da=True))

    res1 = orchestrator.run(graph=graph, session=mem_session)
    assert res1.status == "COMPLETED"
    mem_session.commit()
    pa_count_before = mem_session.query(PageAuthorityScore).count()
    da_count_before = mem_session.query(DomainAuthorityScore).count()
    assert pa_count_before == 5
    assert da_count_before == 2

    def failing_upsert_da(self, records, target_domain=None):
        raise RuntimeError("Simulated disk error during DA write")

    monkeypatch.setattr(CrawlRepository, "upsert_domain_authority_scores", failing_upsert_da)
    res2 = orchestrator.run(graph=graph, session=mem_session)
    assert res2.status == "FAILED"

    assert mem_session.query(PageAuthorityScore).count() == pa_count_before
    assert mem_session.query(DomainAuthorityScore).count() == da_count_before


def test_t5_39_dirty_caller_session_pre_savepoint_flush_survives_da_rollback(mem_session: Session, monkeypatch):
    """T5-39: Pending caller objects are flushed before SAVEPOINT and survive a nested DA rollback."""
    # Caller adds a pending Domain object before calling orchestrator
    caller_domain = Domain(domain_name="caller-pending.com", registered_domain="caller-pending.com", tld="com")
    mem_session.add(caller_domain)

    graph = _build_synthetic_multi_domain_graph()
    orchestrator = PageRankOrchestrator(AuthorityScoringConfig(compute_da=True))

    def failing_upsert_da(self, records, target_domain=None):
        raise IntegrityError("Simulated DA failure", params=None, orig=Exception("da_fail"))

    monkeypatch.setattr(CrawlRepository, "upsert_domain_authority_scores", failing_upsert_da)
    res = orchestrator.run(graph=graph, session=mem_session)

    assert res.status == "FAILED"
    assert mem_session.is_active is True
    # PA and DA writes inside SAVEPOINT rolled back:
    assert mem_session.query(PageAuthorityScore).count() == 0
    assert mem_session.query(DomainAuthorityScore).count() == 0
    # Caller's pending Domain object survived in the outer transaction:
    assert mem_session.query(Domain).filter_by(domain_name="caller-pending.com").count() == 1


# ==============================================================================
# CATEGORY 4: ORCHESTRATOR PIPELINE INTEGRATION TESTS (T5-27 to T5-32)
# ==============================================================================

def test_t5_27_orchestrator_default_runs_pa_only_without_da_table():
    """T5-27: Default AuthorityScoringConfig (compute_da=False) succeeds even if domain_authority_scores table does not exist."""
    engine = create_engine("sqlite:///:memory:")
    # Create ONLY the M4 tables (drop domain_authority_scores)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE domain_authority_scores"))

    insp = inspect(engine)
    assert "domain_authority_scores" not in insp.get_table_names()

    Sess = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with Sess() as sess:
        graph = _build_synthetic_multi_domain_graph()
        orchestrator = PageRankOrchestrator()  # Default compute_da=False
        res = orchestrator.run(graph=graph, session=sess)
        sess.commit()

        assert res.status == "COMPLETED"
        assert res.total_pages_scored == 5
        assert res.total_domains_scored == 0
        assert sess.query(PageAuthorityScore).count() == 5
    engine.dispose()


def test_t5_28_orchestrator_with_da_produces_both_scores(mem_session: Session):
    """T5-28: Explicit compute_da=True computes and persists both PA and DA scores."""
    graph = _build_synthetic_multi_domain_graph()
    orchestrator = PageRankOrchestrator(AuthorityScoringConfig(compute_da=True))
    res = orchestrator.run(graph=graph, session=mem_session)
    mem_session.commit()

    assert res.status == "COMPLETED"
    assert res.total_pages_scored == 5
    assert res.total_domains_scored == 2
    assert res.max_lp_da_score > 0.0
    assert mem_session.query(PageAuthorityScore).count() == 5
    assert mem_session.query(DomainAuthorityScore).count() == 2


def test_t5_29_orchestrator_empty_graph_graceful_handling(mem_session: Session):
    """T5-29: Empty graph completes cleanly with 0 PA and 0 DA rows."""
    empty_graph = PhysicalGraph()
    empty_graph.finalize()
    orchestrator = PageRankOrchestrator(AuthorityScoringConfig(compute_da=True))
    res = orchestrator.run(graph=empty_graph, session=mem_session)

    assert res.status == "COMPLETED"
    assert res.total_pages_scored == 0
    assert res.total_domains_scored == 0


def test_t5_30_stage_7_validation_gate_rejects_invalid_da(mem_session: Session, monkeypatch):
    """T5-30: Pre-persistence validation gate rejects out-of-bounds DA records before opening DB transaction."""
    graph = _build_synthetic_multi_domain_graph()
    orchestrator = PageRankOrchestrator(AuthorityScoringConfig(compute_da=True))

    def corrupted_da(self, pa_records, computed_at=None):
        return [{
            "domain_name": "alpha.com",
            "lp_da_score": 1.5,  # Invalid > 1.0
            "lp_da_display": 150.0,
            "anchor_page_pa": 0.5,
            "top_k_harmonic_pa": 0.5,
            "mean_page_pa": 0.5,
            "breadth_score": 0.5,
            "scored_pages_count": 3,
        }]

    monkeypatch.setattr(PageRankOrchestrator, "_aggregate_domain_authority_scores", corrupted_da)
    res = orchestrator.run(graph=graph, session=mem_session)

    assert res.status == "FAILED"
    assert "Pre-persistence validation gate failed" in (res.error_message or "")
    assert mem_session.query(PageAuthorityScore).count() == 0
    assert mem_session.query(DomainAuthorityScore).count() == 0


def test_t5_31_target_domain_filtering_pipeline(mem_session: Session):
    """T5-31: Target-domain filtering scopes both PA and DA computation and persistence."""
    graph = _build_synthetic_multi_domain_graph()
    orchestrator = PageRankOrchestrator(AuthorityScoringConfig(compute_da=True))
    res = orchestrator.run(graph=graph, target_domain="alpha.com", session=mem_session)
    mem_session.commit()

    assert res.status == "COMPLETED"
    assert res.total_pages_scored == 3
    assert res.total_domains_scored == 1
    da_rows = mem_session.query(DomainAuthorityScore).all()
    assert len(da_rows) == 1
    assert da_rows[0].domain_name == "alpha.com"


def test_t5_32_level1_and_level2_reproducibility_from_persisted_columns(mem_session: Session):
    """T5-32: Persisted DA rows support exact Level 1 and Level 2 mathematical score reconstruction."""
    graph = _build_synthetic_multi_domain_graph()
    orchestrator = PageRankOrchestrator(AuthorityScoringConfig(compute_da=True))
    res = orchestrator.run(graph=graph, session=mem_session)
    mem_session.commit()
    assert res.status == "COMPLETED"

    repo = CrawlRepository(mem_session)
    for da_row in mem_session.query(DomainAuthorityScore).all():
        # Level 1 reconstruction from row columns
        reconstructed_l1 = reconstruct_lp_da_level1(
            anchor_page_pa=da_row.anchor_page_pa,
            top_k_harmonic_pa=da_row.top_k_harmonic_pa,
            breadth_score=da_row.breadth_score,
            mean_page_pa=da_row.mean_page_pa,
            alpha=da_row.alpha,
            beta=da_row.beta,
            gamma=da_row.gamma,
        )
        assert math.isclose(reconstructed_l1, da_row.lp_da_score, abs_tol=1e-15)

        # Level 2 reconstruction from constituent PA rows
        pa_rows = repo.get_authority_scores(domain=da_row.domain_name, limit=1000, internal_only=True)
        pa_scores = [p.lp_pa_score for p in pa_rows]
        comp_l2 = compute_single_domain_authority(
            domain_name=da_row.domain_name,
            scores=pa_scores,
            alpha=da_row.alpha,
            beta=da_row.beta,
            gamma=da_row.gamma,
            top_k=da_row.configured_top_k,
            epsilon=da_row.harmonic_epsilon,
            reference_page_count=da_row.reference_page_count,
        )
        assert math.isclose(comp_l2.lp_da_score, da_row.lp_da_score, abs_tol=1e-15)


# ==============================================================================
# CATEGORY 5: CLI & CANONICAL LOOKUP ISOLATION TESTS (T5-33 to T5-38, T5-40)
# ==============================================================================

def _create_isolated_cli_db(tmp_path, include_da_table: bool = True):
    """Create a temporary SQLite database populated with sample pages and links for CLI testing."""
    db_file = tmp_path / "cli_m5_test.db"
    eng = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(eng)
    if not include_da_table:
        with eng.begin() as conn:
            conn.execute(text("DROP TABLE domain_authority_scores"))

    Sess = sessionmaker(bind=eng, autocommit=False, autoflush=False)
    with Sess() as s:
        ds = DataSource(id="commoncrawl", name="Common Crawl")
        cr = CrawlRun(id=1, source_id="commoncrawl", run_type="cc_targeted_outbound", status="completed")
        d = Domain(id=1, domain_name="alpha.com", registered_domain="alpha.com", tld="com")
        p1 = Page(id=1, domain_id=1, raw_url="https://alpha.com/", normalized_url="https://alpha.com/", http_status=200)
        p2 = Page(id=2, domain_id=1, raw_url="https://alpha.com/about", normalized_url="https://alpha.com/about", http_status=200)
        pl = PageLink(
            id=1,
            source_page_id=2,
            target_page_id=1,
            source_domain_id=1,
            target_domain_id=1,
            is_active=True,
        )
        lo = LinkOccurrence(
            id=1,
            page_link_id=1,
            crawl_run_id=1,
            source_id="commoncrawl",
            raw_target_url="https://alpha.com/",
            normalized_target_url="https://alpha.com/",
            rel_attributes="",
            dom_context="main",
        )
        s.add_all([ds, cr, d, p1, p2, pl, lo])
        s.commit()
    return db_file, eng, Sess


def test_t5_33_cli_compute_default_no_da(tmp_path, monkeypatch):
    """T5-33: CLI 'authority compute --all' defaults to --no-da and succeeds on a 17-table DB."""
    _, eng, Sess = _create_isolated_cli_db(tmp_path, include_da_table=False)
    import app.database.connection as conn_mod
    monkeypatch.setattr(conn_mod, "engine", eng)
    monkeypatch.setattr(conn_mod, "SessionLocal", Sess)

    runner = CliRunner()
    result = runner.invoke(cli_app, ["authority", "compute", "--all"])
    assert result.exit_code == 0, result.output
    assert "Page Authority Scoring Summary" in result.output
    eng.dispose()


def test_t5_34_cli_compute_with_da_success(tmp_path, monkeypatch):
    """T5-34: CLI 'authority compute --all --with-da' computes PA and DA and renders domain leaderboard."""
    _, eng, Sess = _create_isolated_cli_db(tmp_path, include_da_table=True)
    import app.database.connection as conn_mod
    monkeypatch.setattr(conn_mod, "engine", eng)
    monkeypatch.setattr(conn_mod, "SessionLocal", Sess)

    runner = CliRunner()
    result = runner.invoke(cli_app, ["authority", "compute", "--all", "--with-da"])
    assert result.exit_code == 0, result.output
    assert "Total Domains Scored" in result.output
    assert "Authoritative Domains (LP-DA)" in result.output
    assert "alpha.com" in result.output
    eng.dispose()


def test_t5_35_cli_compute_with_da_missing_schema_fails_cleanly(tmp_path, monkeypatch):
    """T5-35: CLI 'authority compute --all --with-da' fails with exit code 1 when M5 table is absent."""
    _, eng, Sess = _create_isolated_cli_db(tmp_path, include_da_table=False)
    import app.database.connection as conn_mod
    monkeypatch.setattr(conn_mod, "engine", eng)
    monkeypatch.setattr(conn_mod, "SessionLocal", Sess)

    runner = CliRunner()
    result = runner.invoke(cli_app, ["authority", "compute", "--all", "--with-da"])
    assert result.exit_code == 1
    assert "domain_authority_scores" in result.output
    eng.dispose()


def test_t5_36_cli_domain_lookup_formatted_output(tmp_path, monkeypatch):
    """T5-36: CLI 'authority domain <domain>' renders the LP-DA profile and constituent pages."""
    _, eng, Sess = _create_isolated_cli_db(tmp_path, include_da_table=True)
    import app.database.connection as conn_mod
    monkeypatch.setattr(conn_mod, "engine", eng)
    monkeypatch.setattr(conn_mod, "SessionLocal", Sess)

    runner = CliRunner()
    comp_res = runner.invoke(cli_app, ["authority", "compute", "--all", "--with-da"])
    assert comp_res.exit_code == 0

    lookup_res = runner.invoke(cli_app, ["authority", "domain", "WWW.ALPHA.COM."])
    assert lookup_res.exit_code == 0, lookup_res.output
    assert "Domain Authority (LP-DA) Profile: alpha.com" in lookup_res.output
    assert "LP-DA Display Score (0-100)" in lookup_res.output
    eng.dispose()


def test_t5_37_cli_domain_lookup_missing_domain_fails_cleanly(tmp_path, monkeypatch):
    """T5-37: CLI 'authority domain <unknown>' exits with code 1 when domain is not found."""
    _, eng, Sess = _create_isolated_cli_db(tmp_path, include_da_table=True)
    import app.database.connection as conn_mod
    monkeypatch.setattr(conn_mod, "engine", eng)
    monkeypatch.setattr(conn_mod, "SessionLocal", Sess)

    runner = CliRunner()
    lookup_res = runner.invoke(cli_app, ["authority", "domain", "unknown-domain.org"])
    assert lookup_res.exit_code == 1
    assert "No Domain Authority score found" in lookup_res.output
    eng.dispose()


def test_t5_38_cli_domain_lookup_read_only_invariance(tmp_path, monkeypatch):
    """T5-38: CLI 'authority domain' connects in read-only mode and leaves DB file hash identical."""
    db_file, eng, Sess = _create_isolated_cli_db(tmp_path, include_da_table=True)
    import app.database.connection as conn_mod
    monkeypatch.setattr(conn_mod, "engine", eng)
    monkeypatch.setattr(conn_mod, "SessionLocal", Sess)

    runner = CliRunner()
    runner.invoke(cli_app, ["authority", "compute", "--all", "--with-da"])
    eng.dispose()

    hash_before = hashlib.sha256(db_file.read_bytes()).hexdigest()
    res = runner.invoke(cli_app, ["authority", "domain", "alpha.com"])
    assert res.exit_code == 0
    hash_after = hashlib.sha256(db_file.read_bytes()).hexdigest()
    assert hash_before == hash_after


def test_t5_40_canonical_domain_lookup_normalization():
    """T5-40: Domain canonicalizer resolves subdomains, uppercase, trailing dots, and URLs to PSL site_boundary."""
    assert canonicalize_domain_lookup("WWW.EXAMPLE.COM.") == "example.com"
    assert canonicalize_domain_lookup("blog.shop.example.com") == "example.com"
    assert canonicalize_domain_lookup("https://www.example.co.uk:8443/products?id=1") == "example.co.uk"
    with pytest.raises(ValueError):
        canonicalize_domain_lookup("   ")
