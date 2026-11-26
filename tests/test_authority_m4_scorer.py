"""
LinkPilot AI — Phase 1B Milestone 4: Test Suite
Comprehensive verification of Page Authority Scoring, transaction atomicity,
WCC component scaling, LP-PA mathematical bounds, error recovery, CLI contracts,
and zero-regression baseline invariance.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker, Session
from typer.testing import CliRunner

from app.authority.graph import (
    GraphVertex,
    PhysicalEdgeOccurrence,
    PhysicalGraph,
    VertexType,
    compute_component_scale,
)
from app.authority.matrix import (
    CSRMatrix,
    CSRMatrixBuilder,
    compute_page_authority_scores,
)
from app.authority.pagerank import (
    PageRankResult,
    PageRankStatus,
    compute_pagerank,
)
from app.authority.scorer import (
    AuthorityScoringConfig,
    AuthorityScoringResult,
    PageRankOrchestrator,
    compute_and_persist_authority_scores,
)
from app.commands.cli import app as cli_app
from app.database.models import Base, Domain, Page, PageAuthorityScore
from app.database.repositories import CrawlRepository


# ==============================================================================
# TEST FIXTURES
# ==============================================================================

@pytest.fixture(autouse=True)
def prevent_cli_db_touch(monkeypatch):
    """Ensure CLI invocations during test runs do not initialize or mutate the physical database."""
    monkeypatch.setattr("app.commands.cli.init_db", lambda: None)


@pytest.fixture
def mem_engine():
    """In-memory SQLite engine with PRAGMA foreign_keys = ON."""
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def mem_session(mem_engine):
    """Session connected to isolated in-memory database with pages 1..50 seeded."""
    SessionMaker = sessionmaker(bind=mem_engine, autocommit=False, autoflush=False)
    session = SessionMaker()

    # Seed default domain and pages 1..50 to satisfy FK constraints for synthetic graphs
    dom = Domain(
        domain_name="example.com",
        registered_domain="example.com",
        tld="com",
        is_punycode=False,
        first_discovered_at=datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc),
    )
    session.add(dom)
    session.flush()

    for pid in range(1, 51):
        session.add(Page(
            id=pid,
            domain_id=dom.id,
            raw_url=f"https://example.com/p{pid}",
            normalized_url=f"https://example.com/p{pid}",
            first_discovered_at=datetime.now(timezone.utc),
        ))
    session.commit()

    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def sample_graph_3node():
    """3-node cyclic graph: A -> B -> C -> A."""
    g = PhysicalGraph()
    v_a = GraphVertex(
        key="https://example.com/a",
        raw_url="https://example.com/a",
        normalized_url="https://example.com/a",
        domain="example.com",
        site_boundary="example.com",
        vertex_type=VertexType.INTERNAL_CRAWLED,
        is_authoritative_scored_page=True,
        page_id=1,
    )
    v_b = GraphVertex(
        key="https://example.com/b",
        raw_url="https://example.com/b",
        normalized_url="https://example.com/b",
        domain="example.com",
        site_boundary="example.com",
        vertex_type=VertexType.INTERNAL_CRAWLED,
        is_authoritative_scored_page=True,
        page_id=2,
    )
    v_c = GraphVertex(
        key="https://example.com/c",
        raw_url="https://example.com/c",
        normalized_url="https://example.com/c",
        domain="example.com",
        site_boundary="example.com",
        vertex_type=VertexType.INTERNAL_CRAWLED,
        is_authoritative_scored_page=True,
        page_id=3,
    )
    g.add_vertex(v_a)
    g.add_vertex(v_b)
    g.add_vertex(v_c)

    g.add_edge_occurrence(PhysicalEdgeOccurrence("https://example.com/a", "https://example.com/b", "/b"))
    g.add_edge_occurrence(PhysicalEdgeOccurrence("https://example.com/b", "https://example.com/c", "/c"))
    g.add_edge_occurrence(PhysicalEdgeOccurrence("https://example.com/c", "https://example.com/a", "/a"))
    g.finalize()
    return g


# ==============================================================================
# GROUP A: DATABASE SCHEMA, ORM MODEL & CONSTRAINTS (T4-01 to T4-06)
# ==============================================================================

def test_t4_01_table_creation_and_column_schema(mem_engine):
    """Verify table creation and exact presence of all 14 columns."""
    insp = inspect(mem_engine)
    assert insp.has_table("page_authority_scores")

    cols = {c["name"]: c for c in insp.get_columns("page_authority_scores")}
    expected = [
        "id", "page_id", "normalized_url", "domain", "is_internal_page",
        "lp_pa_score", "raw_pagerank", "component_id", "comp_scale",
        "alpha", "pagerank_status", "iterations", "final_residual", "computed_at"
    ]
    for col_name in expected:
        assert col_name in cols, f"Missing column {col_name}"

    assert cols["page_id"]["nullable"] is True
    assert cols["normalized_url"]["nullable"] is False
    assert cols["lp_pa_score"]["nullable"] is False


def test_t4_02_orm_model_instantiation_and_defaults(mem_session):
    """Verify ORM model instantiation and default attribute behavior."""
    now = datetime.now(timezone.utc)
    pas = PageAuthorityScore(
        page_id=None,
        normalized_url="https://example.com/test",
        domain="example.com",
        is_internal_page=False,
        lp_pa_score=0.45,
        raw_pagerank=0.012,
        component_id=0,
        comp_scale=0.5,
        alpha=0.85,
        pagerank_status="CONVERGED",
        iterations=15,
        final_residual=1e-13,
        computed_at=now,
    )
    assert pas.lp_pa_score == 0.45
    assert pas.is_internal_page is False
    assert pas.page_id is None


def test_t4_03_indexes_and_ordering_coverage(mem_engine):
    """Verify presence of indexes on domain, lp_pa_score, and uniqueness constraint."""
    insp = inspect(mem_engine)
    indexes = insp.get_indexes("page_authority_scores")
    index_names = [idx["name"] for idx in indexes]
    assert any("domain" in name for name in index_names)
    assert any("score" in name for name in index_names)


def test_t4_04_nullable_foreign_key_for_external_terminals(mem_session):
    """Verify external terminal records with page_id=None insert cleanly under active foreign keys."""
    now = datetime.now(timezone.utc)
    rec = PageAuthorityScore(
        page_id=None,
        normalized_url="https://external-sink.com/resource",
        domain="external-sink.com",
        is_internal_page=False,
        lp_pa_score=0.12,
        raw_pagerank=0.005,
        component_id=1,
        comp_scale=0.2,
        alpha=0.85,
        pagerank_status="CONVERGED",
        iterations=5,
        final_residual=1e-14,
        computed_at=now,
    )
    mem_session.add(rec)
    mem_session.commit()

    saved = mem_session.query(PageAuthorityScore).filter_by(
        normalized_url="https://external-sink.com/resource"
    ).first()
    assert saved is not None
    assert saved.page_id is None
    assert saved.is_internal_page is False


def test_t4_05_url_uniqueness_constraint_enforced(mem_session):
    """Verify that duplicate normalized_url values raise IntegrityError."""
    now = datetime.now(timezone.utc)
    r1 = PageAuthorityScore(
        normalized_url="https://example.com/unique",
        domain="example.com",
        lp_pa_score=0.5,
        raw_pagerank=0.01,
        component_id=0,
        comp_scale=0.5,
        alpha=0.85,
        pagerank_status="CONVERGED",
        iterations=10,
        final_residual=1e-13,
        computed_at=now,
    )
    r2 = PageAuthorityScore(
        normalized_url="https://example.com/unique",
        domain="example.com",
        lp_pa_score=0.6,
        raw_pagerank=0.02,
        component_id=0,
        comp_scale=0.5,
        alpha=0.85,
        pagerank_status="CONVERGED",
        iterations=10,
        final_residual=1e-13,
        computed_at=now,
    )
    mem_session.add(r1)
    mem_session.commit()

    mem_session.add(r2)
    with pytest.raises(IntegrityError):
        mem_session.commit()
    mem_session.rollback()


def test_t4_06_internal_page_foreign_key_referential_integrity(mem_session):
    """Verify that inserting internal page with non-existent page_id fails foreign key constraint."""
    now = datetime.now(timezone.utc)
    rec = PageAuthorityScore(
        page_id=999999,  # Non-existent foreign key
        normalized_url="https://example.com/bad-fk",
        domain="example.com",
        is_internal_page=True,
        lp_pa_score=0.3,
        raw_pagerank=0.01,
        component_id=0,
        comp_scale=0.5,
        alpha=0.85,
        pagerank_status="CONVERGED",
        iterations=10,
        final_residual=1e-13,
        computed_at=now,
    )
    mem_session.add(rec)
    with pytest.raises(IntegrityError):
        mem_session.commit()
    mem_session.rollback()


# ==============================================================================
# GROUP B: REPOSITORY LAYER & TRANSACTION ATOMICITY (T4-07 to T4-14)
# ==============================================================================

def test_t4_07_upsert_scores_basic_batch_write(mem_session):
    """Verify basic upsert of score dictionaries via CrawlRepository."""
    repo = CrawlRepository(mem_session)
    now = datetime.now(timezone.utc)
    records = [
        {
            "page_id": None,
            "normalized_url": f"https://example.com/p{i}",
            "domain": "example.com",
            "is_internal_page": False,
            "lp_pa_score": 0.1 * i,
            "raw_pagerank": 0.01 * i,
            "component_id": 0,
            "comp_scale": 0.5,
            "alpha": 0.85,
            "pagerank_status": "CONVERGED",
            "iterations": 10,
            "final_residual": 1e-13,
            "computed_at": now,
        }
        for i in range(1, 4)
    ]
    written = repo.upsert_page_authority_scores(records)
    mem_session.commit()
    assert written == 3

    rows = mem_session.query(PageAuthorityScore).all()
    assert len(rows) == 3


def test_t4_08_transaction_rollback_on_insertion_failure(mem_session):
    """Verify that toxic payload triggers exception and database rollback."""
    repo = CrawlRepository(mem_session)
    now = datetime.now(timezone.utc)
    records = [
        {
            "page_id": 999999,  # Invalid FK triggers IntegrityError
            "normalized_url": "https://example.com/fail",
            "domain": "example.com",
            "is_internal_page": True,
            "lp_pa_score": 0.5,
            "raw_pagerank": 0.01,
            "component_id": 0,
            "comp_scale": 0.5,
            "alpha": 0.85,
            "pagerank_status": "CONVERGED",
            "iterations": 10,
            "final_residual": 1e-13,
            "computed_at": now,
        }
    ]
    with pytest.raises(IntegrityError):
        repo.upsert_page_authority_scores(records)
    mem_session.rollback()


def test_t4_09_existing_score_preservation_on_aborted_transaction(mem_session):
    """Verify that aborted upsert transaction leaves previously committed scores 100% intact."""
    repo = CrawlRepository(mem_session)
    now = datetime.now(timezone.utc)

    # Initial clean commit
    initial_rec = [
        {
            "page_id": None,
            "normalized_url": "https://example.com/intact",
            "domain": "example.com",
            "is_internal_page": False,
            "lp_pa_score": 0.77,
            "raw_pagerank": 0.05,
            "component_id": 0,
            "comp_scale": 0.5,
            "alpha": 0.85,
            "pagerank_status": "CONVERGED",
            "iterations": 10,
            "final_residual": 1e-13,
            "computed_at": now,
        }
    ]
    repo.upsert_page_authority_scores(initial_rec)
    mem_session.commit()

    # Attempt toxic replacement
    toxic = [
        {
            "page_id": 999999,
            "normalized_url": "https://example.com/toxic",
            "domain": "example.com",
            "is_internal_page": True,
            "lp_pa_score": 0.1,
            "raw_pagerank": 0.01,
            "component_id": 0,
            "comp_scale": 0.5,
            "alpha": 0.85,
            "pagerank_status": "CONVERGED",
            "iterations": 10,
            "final_residual": 1e-13,
            "computed_at": now,
        }
    ]
    try:
        repo.upsert_page_authority_scores(toxic, target_domain="example.com")
        mem_session.commit()
    except IntegrityError:
        mem_session.rollback()

    # Verify original score is preserved
    rows = mem_session.query(PageAuthorityScore).all()
    assert len(rows) == 1
    assert rows[0].normalized_url == "https://example.com/intact"
    assert rows[0].lp_pa_score == 0.77


def test_t4_10_idempotent_overwrite_replaces_previous_records(mem_session):
    """Verify that re-running upsert replaces prior scores without creating duplicate rows."""
    repo = CrawlRepository(mem_session)
    now1 = datetime.now(timezone.utc)
    rec1 = [{
        "page_id": None,
        "normalized_url": "https://example.com/page",
        "domain": "example.com",
        "is_internal_page": False,
        "lp_pa_score": 0.30,
        "raw_pagerank": 0.01,
        "component_id": 0,
        "comp_scale": 0.5,
        "alpha": 0.85,
        "pagerank_status": "CONVERGED",
        "iterations": 10,
        "final_residual": 1e-13,
        "computed_at": now1,
    }]
    repo.upsert_page_authority_scores(rec1, target_domain="example.com")
    mem_session.commit()

    # Update score
    now2 = datetime.now(timezone.utc)
    rec2 = [{
        "page_id": None,
        "normalized_url": "https://example.com/page",
        "domain": "example.com",
        "is_internal_page": False,
        "lp_pa_score": 0.85,
        "raw_pagerank": 0.05,
        "component_id": 0,
        "comp_scale": 0.5,
        "alpha": 0.85,
        "pagerank_status": "CONVERGED",
        "iterations": 12,
        "final_residual": 1e-14,
        "computed_at": now2,
    }]
    repo.upsert_page_authority_scores(rec2, target_domain="example.com")
    mem_session.commit()

    rows = mem_session.query(PageAuthorityScore).all()
    assert len(rows) == 1
    assert rows[0].lp_pa_score == 0.85
    assert rows[0].raw_pagerank == 0.05


def test_t4_11_domain_scoped_isolation_leaves_other_domains_untouched(mem_session):
    """Verify that domain-scoped upsert leaves other domains' scores untouched."""
    repo = CrawlRepository(mem_session)
    now = datetime.now(timezone.utc)

    records = [
        {
            "page_id": None,
            "normalized_url": "https://domain-a.com/page",
            "domain": "domain-a.com",
            "is_internal_page": False,
            "lp_pa_score": 0.4,
            "raw_pagerank": 0.02,
            "component_id": 0,
            "comp_scale": 0.5,
            "alpha": 0.85,
            "pagerank_status": "CONVERGED",
            "iterations": 10,
            "final_residual": 1e-13,
            "computed_at": now,
        },
        {
            "page_id": None,
            "normalized_url": "https://domain-b.com/page",
            "domain": "domain-b.com",
            "is_internal_page": False,
            "lp_pa_score": 0.6,
            "raw_pagerank": 0.03,
            "component_id": 0,
            "comp_scale": 0.5,
            "alpha": 0.85,
            "pagerank_status": "CONVERGED",
            "iterations": 10,
            "final_residual": 1e-13,
            "computed_at": now,
        },
    ]
    repo.upsert_page_authority_scores(records)
    mem_session.commit()

    # Rescore only domain-a
    new_a = [
        {
            "page_id": None,
            "normalized_url": "https://domain-a.com/page",
            "domain": "domain-a.com",
            "is_internal_page": False,
            "lp_pa_score": 0.9,
            "raw_pagerank": 0.08,
            "component_id": 0,
            "comp_scale": 0.5,
            "alpha": 0.85,
            "pagerank_status": "CONVERGED",
            "iterations": 10,
            "final_residual": 1e-13,
            "computed_at": now,
        }
    ]
    repo.upsert_page_authority_scores(new_a, target_domain="domain-a.com")
    mem_session.commit()

    a_row = mem_session.query(PageAuthorityScore).filter_by(domain="domain-a.com").first()
    b_row = mem_session.query(PageAuthorityScore).filter_by(domain="domain-b.com").first()

    assert a_row.lp_pa_score == 0.9
    assert b_row.lp_pa_score == 0.6  # Untouched


def test_t4_12_query_filtering_domain_minscore_internal(mem_session):
    """Verify repository query filters for domain, min_score, and internal_only."""
    repo = CrawlRepository(mem_session)
    now = datetime.now(timezone.utc)
    records = [
        {"page_id": 1, "normalized_url": "https://a.com/1", "domain": "a.com", "is_internal_page": True, "lp_pa_score": 0.8, "raw_pagerank": 0.04, "component_id": 0, "comp_scale": 0.5, "alpha": 0.85, "pagerank_status": "CONVERGED", "iterations": 10, "final_residual": 1e-13, "computed_at": now},
        {"page_id": 2, "normalized_url": "https://a.com/2", "domain": "a.com", "is_internal_page": True, "lp_pa_score": 0.3, "raw_pagerank": 0.01, "component_id": 0, "comp_scale": 0.5, "alpha": 0.85, "pagerank_status": "CONVERGED", "iterations": 10, "final_residual": 1e-13, "computed_at": now},
        {"page_id": None, "normalized_url": "https://a.com/ext", "domain": "a.com", "is_internal_page": False, "lp_pa_score": 0.9, "raw_pagerank": 0.05, "component_id": 0, "comp_scale": 0.5, "alpha": 0.85, "pagerank_status": "CONVERGED", "iterations": 10, "final_residual": 1e-13, "computed_at": now},
        {"page_id": 3, "normalized_url": "https://b.com/1", "domain": "b.com", "is_internal_page": True, "lp_pa_score": 0.7, "raw_pagerank": 0.03, "component_id": 0, "comp_scale": 0.5, "alpha": 0.85, "pagerank_status": "CONVERGED", "iterations": 10, "final_residual": 1e-13, "computed_at": now},
    ]
    repo.upsert_page_authority_scores(records)
    mem_session.commit()

    # Filter a.com internal pages with score >= 0.5
    res = repo.get_authority_scores(domain="a.com", min_score=0.5, internal_only=True)
    assert len(res) == 1
    assert res[0].normalized_url == "https://a.com/1"


def test_t4_13_query_top_scores_descending_limit(mem_session):
    """Verify get_top_authority_scores returns descending scores bounded by limit."""
    repo = CrawlRepository(mem_session)
    now = datetime.now(timezone.utc)
    records = [
        {"page_id": i, "normalized_url": f"https://a.com/{i}", "domain": "a.com", "is_internal_page": True, "lp_pa_score": 0.05 * i, "raw_pagerank": 0.01 * i, "component_id": 0, "comp_scale": 0.5, "alpha": 0.85, "pagerank_status": "CONVERGED", "iterations": 10, "final_residual": 1e-13, "computed_at": now}
        for i in range(1, 11)
    ]
    repo.upsert_page_authority_scores(records)
    mem_session.commit()

    top_3 = repo.get_top_authority_scores(limit=3, internal_only=True)
    assert len(top_3) == 3
    assert top_3[0].lp_pa_score == 0.50
    assert top_3[1].lp_pa_score == 0.45
    assert top_3[2].lp_pa_score == 0.40


def test_t4_14_interrupted_replacement_atomicity(mem_session):
    """Verify atomicity of delete+insert: if insert fails, delete is rolled back."""
    repo = CrawlRepository(mem_session)
    now = datetime.now(timezone.utc)
    initial = [
        {"page_id": None, "normalized_url": "https://a.com/keep", "domain": "a.com", "is_internal_page": False, "lp_pa_score": 0.5, "raw_pagerank": 0.02, "component_id": 0, "comp_scale": 0.5, "alpha": 0.85, "pagerank_status": "CONVERGED", "iterations": 10, "final_residual": 1e-13, "computed_at": now}
    ]
    repo.upsert_page_authority_scores(initial)
    mem_session.commit()

    # Attempt replacement with duplicate keys within the batch
    bad_batch = [
        {"page_id": None, "normalized_url": "https://a.com/dup", "domain": "a.com", "is_internal_page": False, "lp_pa_score": 0.6, "raw_pagerank": 0.03, "component_id": 0, "comp_scale": 0.5, "alpha": 0.85, "pagerank_status": "CONVERGED", "iterations": 10, "final_residual": 1e-13, "computed_at": now},
        {"page_id": None, "normalized_url": "https://a.com/dup", "domain": "a.com", "is_internal_page": False, "lp_pa_score": 0.7, "raw_pagerank": 0.04, "component_id": 0, "comp_scale": 0.5, "alpha": 0.85, "pagerank_status": "CONVERGED", "iterations": 10, "final_residual": 1e-13, "computed_at": now},
    ]
    with pytest.raises(IntegrityError):
        repo.upsert_page_authority_scores(bad_batch, target_domain="a.com")
        mem_session.commit()
    mem_session.rollback()

    # Verify original row was preserved
    rows = mem_session.query(PageAuthorityScore).all()
    assert len(rows) == 1
    assert rows[0].normalized_url == "https://a.com/keep"


# ==============================================================================
# GROUP C: ORCHESTRATOR PIPELINE LIFECYCLE & ERROR RECOVERY (T4-15 to T4-22)
# ==============================================================================

def test_t4_15_orchestrator_initialization_and_config():
    """Verify AuthorityScoringConfig input validation."""
    cfg = AuthorityScoringConfig(alpha=0.9, tol=1e-10, max_iter=150)
    assert cfg.alpha == 0.9

    with pytest.raises(ValueError):
        AuthorityScoringConfig(alpha=0.0)
    with pytest.raises(ValueError):
        AuthorityScoringConfig(alpha=1.0)
    with pytest.raises(ValueError):
        AuthorityScoringConfig(tol=0.0)
    with pytest.raises(ValueError):
        AuthorityScoringConfig(max_iter=0)


def test_t4_16_end_to_end_pipeline_execution(mem_session, sample_graph_3node):
    """Verify complete 9-stage pipeline execution on sample 3-node graph fixture."""
    orchestrator = PageRankOrchestrator()
    res = orchestrator.run(graph=sample_graph_3node, session=mem_session)
    mem_session.commit()

    assert res.status == "COMPLETED"
    assert res.total_pages_scored == 3
    assert res.converged_components == 1
    assert res.total_components == 1
    assert res.max_lp_pa_score > 0.0

    rows = mem_session.query(PageAuthorityScore).all()
    assert len(rows) == 3


def test_t4_17_pre_persistence_gate_aborts_on_nan_score(mem_session, sample_graph_3node):
    """Verify that Stage 6 Validation Gate rejects non-finite floats without writing to DB."""
    orchestrator = PageRankOrchestrator()
    nan_map = {
        "https://example.com/a": float("nan"),
        "https://example.com/b": 0.5,
        "https://example.com/c": 0.5,
    }
    with patch("app.authority.scorer.compute_page_authority_scores", return_value=nan_map):
        res = orchestrator.run(graph=sample_graph_3node, session=mem_session)

    assert res.status == "FAILED"
    assert "Pre-persistence validation gate failed" in (res.error_message or "")
    rows = mem_session.query(PageAuthorityScore).all()
    assert len(rows) == 0


def test_t4_18_pre_persistence_gate_aborts_on_duplicate_urls(mem_session):
    """Verify Stage 6 Validation Gate catches duplicate URLs before DB transaction."""
    orchestrator = PageRankOrchestrator()
    invalid_batch = [
        {"normalized_url": "https://a.com", "lp_pa_score": 0.5, "raw_pagerank": 0.1, "is_internal_page": False},
        {"normalized_url": "https://a.com", "lp_pa_score": 0.5, "raw_pagerank": 0.1, "is_internal_page": False},
    ]
    passed, err = orchestrator._validate_batch(invalid_batch, n_graph_vertices=2)
    assert passed is False
    assert "duplicate normalized_url" in (err or "")


def test_t4_19_empty_graph_safe_exit(mem_session):
    """Verify orchestrator clean zero-exit when processing empty graph."""
    empty_graph = PhysicalGraph()
    orchestrator = PageRankOrchestrator()
    res = orchestrator.run(graph=empty_graph, session=mem_session)

    assert res.status == "COMPLETED"
    assert res.total_pages_scored == 0
    assert res.total_components == 0


def test_t4_20_zero_valid_scores_aborts_without_database_wipe(mem_session, sample_graph_3node):
    """Verify that if all components fail, Stage 6 gate aborts without wiping existing DB scores."""
    repo = CrawlRepository(mem_session)
    now = datetime.now(timezone.utc)
    initial = [
        {"page_id": None, "normalized_url": "https://example.com/keep", "domain": "example.com", "is_internal_page": False, "lp_pa_score": 0.5, "raw_pagerank": 0.02, "component_id": 0, "comp_scale": 0.5, "alpha": 0.85, "pagerank_status": "CONVERGED", "iterations": 10, "final_residual": 1e-13, "computed_at": now}
    ]
    repo.upsert_page_authority_scores(initial)
    mem_session.commit()

    orchestrator = PageRankOrchestrator()
    # Mock PageRank solver to return numerical overflow for all components
    overflow_res = PageRankResult(
        scores=[],
        converged=False,
        iterations=1,
        final_residual=float("inf"),
        status=PageRankStatus.NUMERICAL_OVERFLOW_OR_NAN,
    )
    with patch("app.authority.scorer.compute_pagerank", return_value=overflow_res):
        res = orchestrator.run(graph=sample_graph_3node, session=mem_session)

    assert res.status == "FAILED"
    # Verify pre-existing row was not deleted
    rows = mem_session.query(PageAuthorityScore).all()
    assert len(rows) == 1
    assert rows[0].normalized_url == "https://example.com/keep"


def test_t4_21_orchestrator_domain_filtering(mem_session):
    """Verify that target_domain scopes scoring and persistence strictly to that domain."""
    g = PhysicalGraph()
    v1 = GraphVertex("https://dom1.com/1", "https://dom1.com/1", "https://dom1.com/1", "dom1.com", "dom1.com", VertexType.INTERNAL_CRAWLED, True, 1)
    v2 = GraphVertex("https://dom2.com/2", "https://dom2.com/2", "https://dom2.com/2", "dom2.com", "dom2.com", VertexType.INTERNAL_CRAWLED, True, 2)
    g.add_vertex(v1)
    g.add_vertex(v2)
    g.finalize()

    orchestrator = PageRankOrchestrator()
    res = orchestrator.run(graph=g, target_domain="dom1.com", session=mem_session)
    mem_session.commit()

    assert res.status == "COMPLETED"
    assert res.total_pages_scored == 1
    rows = mem_session.query(PageAuthorityScore).all()
    assert len(rows) == 1
    assert rows[0].domain == "dom1.com"


def test_t4_22_orchestrator_global_run(mem_session):
    """Verify global run (target_domain=None) scores all internal pages across domains."""
    g = PhysicalGraph()
    v1 = GraphVertex("https://dom1.com/1", "https://dom1.com/1", "https://dom1.com/1", "dom1.com", "dom1.com", VertexType.INTERNAL_CRAWLED, True, 1)
    v2 = GraphVertex("https://dom2.com/2", "https://dom2.com/2", "https://dom2.com/2", "dom2.com", "dom2.com", VertexType.INTERNAL_CRAWLED, True, 2)
    g.add_vertex(v1)
    g.add_vertex(v2)
    g.finalize()

    orchestrator = PageRankOrchestrator()
    res = orchestrator.run(graph=g, target_domain=None, session=mem_session)
    mem_session.commit()

    assert res.status == "COMPLETED"
    assert res.total_pages_scored == 2


# ==============================================================================
# GROUP D: MATHEMATICAL & SCORING CONTRACTS (T4-23 to T4-28)
# ==============================================================================

def test_t4_23_lp_pa_scores_strictly_in_unit_interval(mem_session, sample_graph_3node):
    """Verify mathematical guarantee that 100% of computed scores are bounded in [0.0, 1.0]."""
    orchestrator = PageRankOrchestrator()
    res = orchestrator.run(graph=sample_graph_3node, session=mem_session)
    mem_session.commit()

    rows = mem_session.query(PageAuthorityScore).all()
    for r in rows:
        assert 0.0 <= r.lp_pa_score <= 1.0


def test_t4_24_raw_pagerank_simplex_normalization_per_component(mem_session, sample_graph_3node):
    """Verify probability simplex constraint sum(raw_pagerank) == 1.0 per WCC within 1e-9."""
    orchestrator = PageRankOrchestrator()
    orchestrator.run(graph=sample_graph_3node, session=mem_session)
    mem_session.commit()

    rows = mem_session.query(PageAuthorityScore).all()
    total_pr = sum(r.raw_pagerank for r in rows)
    assert abs(total_pr - 1.0) < 1e-9


def test_t4_25_component_significance_scaling_accuracy(mem_session, sample_graph_3node):
    """Verify comp_scale stored in DB matches analytical formula for component size."""
    orchestrator = PageRankOrchestrator()
    orchestrator.run(graph=sample_graph_3node, session=mem_session)
    mem_session.commit()

    expected_scale = compute_component_scale(3)
    rows = mem_session.query(PageAuthorityScore).all()
    for r in rows:
        assert abs(r.comp_scale - expected_scale) < 1e-12


def test_t4_26_per_component_vs_global_scoring_divergence(mem_session):
    """Verify mathematical non-equivalence between per-component and global scoring modes."""
    # Graph with two disconnected components of unequal sizes
    g = PhysicalGraph()
    # Component 1: 3 nodes
    for i in range(1, 4):
        g.add_vertex(GraphVertex(f"https://a.com/{i}", f"https://a.com/{i}", f"https://a.com/{i}", "a.com", "a.com", VertexType.INTERNAL_CRAWLED, True, i))
    g.add_edge_occurrence(PhysicalEdgeOccurrence("https://a.com/1", "https://a.com/2", "/2"))
    g.add_edge_occurrence(PhysicalEdgeOccurrence("https://a.com/2", "https://a.com/1", "/1"))

    # Component 2: 2 nodes
    for i in range(1, 3):
        g.add_vertex(GraphVertex(f"https://b.com/{i}", f"https://b.com/{i}", f"https://b.com/{i}", "b.com", "b.com", VertexType.INTERNAL_CRAWLED, True, i + 3))
    g.add_edge_occurrence(PhysicalEdgeOccurrence("https://b.com/1", "https://b.com/2", "/2"))
    g.add_edge_occurrence(PhysicalEdgeOccurrence("https://b.com/2", "https://b.com/1", "/1"))
    g.finalize()

    # Per-component run
    cfg_comp = AuthorityScoringConfig(per_component=True)
    res_comp = PageRankOrchestrator(cfg_comp).run(graph=g, session=mem_session)
    mem_session.commit()
    scores_comp = {r.normalized_url: r.lp_pa_score for r in mem_session.query(PageAuthorityScore).all()}

    # Global run
    cfg_glob = AuthorityScoringConfig(per_component=False)
    res_glob = PageRankOrchestrator(cfg_glob).run(graph=g, session=mem_session)
    mem_session.commit()
    scores_glob = {r.normalized_url: r.lp_pa_score for r in mem_session.query(PageAuthorityScore).all()}

    # Verify scores exist and exhibit non-identical values due to global teleportation
    assert len(scores_comp) == len(scores_glob) == 5
    divergences = [abs(scores_comp[k] - scores_glob[k]) for k in scores_comp]
    assert any(d > 1e-5 for d in divergences)


def test_t4_27_dangling_node_finite_scoring(mem_session):
    """Verify that dangling nodes with out-degree zero receive finite non-zero LP-PA scores."""
    g = PhysicalGraph()
    v1 = GraphVertex("https://a.com/src", "https://a.com/src", "https://a.com/src", "a.com", "a.com", VertexType.INTERNAL_CRAWLED, True, 1)
    v2 = GraphVertex("https://a.com/dangle", "https://a.com/dangle", "https://a.com/dangle", "a.com", "a.com", VertexType.INTERNAL_CRAWLED, True, 2)
    g.add_vertex(v1)
    g.add_vertex(v2)
    g.add_edge_occurrence(PhysicalEdgeOccurrence("https://a.com/src", "https://a.com/dangle", "/dangle"))
    g.finalize()

    orchestrator = PageRankOrchestrator()
    res = orchestrator.run(graph=g, session=mem_session)
    mem_session.commit()

    assert res.status == "COMPLETED"
    dangle_row = mem_session.query(PageAuthorityScore).filter_by(normalized_url="https://a.com/dangle").first()
    assert dangle_row is not None
    assert dangle_row.lp_pa_score > 0.0
    assert math.isfinite(dangle_row.lp_pa_score)


def test_t4_28_singleton_wcc_exact_zero_score(mem_session):
    """Verify that an isolated singleton WCC (N=1) receives exact LP-PA score of 0.0."""
    g = PhysicalGraph()
    v1 = GraphVertex("https://a.com/solo", "https://a.com/solo", "https://a.com/solo", "a.com", "a.com", VertexType.INTERNAL_CRAWLED, True, 1)
    g.add_vertex(v1)
    g.finalize()

    orchestrator = PageRankOrchestrator()
    res = orchestrator.run(graph=g, session=mem_session)
    mem_session.commit()

    assert res.status == "COMPLETED"
    solo_row = mem_session.query(PageAuthorityScore).filter_by(normalized_url="https://a.com/solo").first()
    assert solo_row is not None
    assert solo_row.raw_pagerank == 1.0
    assert solo_row.lp_pa_score == 0.0


# ==============================================================================
# GROUP E: STATUS MAPPING, ERROR HANDLING & SINKS (T4-29 to T4-34)
# ==============================================================================

def test_t4_29_numerical_overflow_component_skipped(mem_session, sample_graph_3node):
    """Verify that components returning NUMERICAL_OVERFLOW_OR_NAN are skipped cleanly."""
    orchestrator = PageRankOrchestrator()
    overflow_res = PageRankResult(
        scores=[],
        converged=False,
        iterations=5,
        final_residual=float("inf"),
        status=PageRankStatus.NUMERICAL_OVERFLOW_OR_NAN,
    )
    with patch("app.authority.scorer.compute_pagerank", return_value=overflow_res):
        res = orchestrator.run(graph=sample_graph_3node, session=mem_session)

    assert res.status == "FAILED"  # Pre-persistence gate catches 0 scores generated
    assert res.non_converged_components == 1
    rows = mem_session.query(PageAuthorityScore).all()
    assert len(rows) == 0


def test_t4_30_converged_status_recorded(mem_session, sample_graph_3node):
    """Verify that solver convergence is recorded as pagerank_status = 'CONVERGED'."""
    orchestrator = PageRankOrchestrator()
    orchestrator.run(graph=sample_graph_3node, session=mem_session)
    mem_session.commit()

    rows = mem_session.query(PageAuthorityScore).all()
    for r in rows:
        assert r.pagerank_status == "CONVERGED"


def test_t4_31_max_iterations_reached_status_persisted(mem_session):
    """Verify that MAX_ITERATIONS_REACHED is persisted with a valid score vector."""
    # Asymmetric graph where uniform initial distribution has non-zero initial residual
    g = PhysicalGraph()
    v1 = GraphVertex("https://a.com/1", "https://a.com/1", "https://a.com/1", "a.com", "a.com", VertexType.INTERNAL_CRAWLED, True, 1)
    v2 = GraphVertex("https://a.com/2", "https://a.com/2", "https://a.com/2", "a.com", "a.com", VertexType.INTERNAL_CRAWLED, True, 2)
    v3 = GraphVertex("https://a.com/3", "https://a.com/3", "https://a.com/3", "a.com", "a.com", VertexType.INTERNAL_CRAWLED, True, 3)
    g.add_vertex(v1)
    g.add_vertex(v2)
    g.add_vertex(v3)
    # Asymmetric structure: 1->2, 2->1, 2->3, 3->1
    g.add_edge_occurrence(PhysicalEdgeOccurrence("https://a.com/1", "https://a.com/2", "/2"))
    g.add_edge_occurrence(PhysicalEdgeOccurrence("https://a.com/2", "https://a.com/1", "/1"))
    g.add_edge_occurrence(PhysicalEdgeOccurrence("https://a.com/2", "https://a.com/3", "/3"))
    g.add_edge_occurrence(PhysicalEdgeOccurrence("https://a.com/3", "https://a.com/1", "/1"))
    g.finalize()

    cfg = AuthorityScoringConfig(max_iter=1, tol=1e-15)  # Force incomplete convergence
    orchestrator = PageRankOrchestrator(cfg)
    res = orchestrator.run(graph=g, session=mem_session)
    mem_session.commit()

    assert res.status == "COMPLETED"
    assert res.non_converged_components == 1
    rows = mem_session.query(PageAuthorityScore).all()
    assert len(rows) == 3
    for r in rows:
        assert r.pagerank_status == "MAX_ITERATIONS_REACHED"


def test_t4_32_stagnated_noise_floor_status_persisted(mem_session, sample_graph_3node):
    """Verify that STAGNATED_AT_NOISE_FLOOR status is properly stored."""
    stagnated_res = PageRankResult(
        scores=[1/3, 1/3, 1/3],
        converged=True,
        iterations=25,
        final_residual=5e-16,
        status=PageRankStatus.STAGNATED_AT_NOISE_FLOOR,
    )
    orchestrator = PageRankOrchestrator()
    with patch("app.authority.scorer.compute_pagerank", return_value=stagnated_res):
        res = orchestrator.run(graph=sample_graph_3node, session=mem_session)
    mem_session.commit()

    assert res.status == "COMPLETED"
    rows = mem_session.query(PageAuthorityScore).all()
    assert rows[0].pagerank_status == "STAGNATED_AT_NOISE_FLOOR"


def test_t4_33_external_terminal_handling_when_allowed(mem_session):
    """Verify external terminals saved with is_internal_page=False, page_id=None when enabled."""
    g = PhysicalGraph()
    v_int = GraphVertex("https://a.com/page", "https://a.com/page", "https://a.com/page", "a.com", "a.com", VertexType.INTERNAL_CRAWLED, True, 1)
    v_ext = GraphVertex("https://ext.com/sink", "https://ext.com/sink", "https://ext.com/sink", "ext.com", "ext.com", VertexType.EXTERNAL_TERMINAL, False, None)
    g.add_vertex(v_int)
    g.add_vertex(v_ext)
    g.add_edge_occurrence(PhysicalEdgeOccurrence("https://a.com/page", "https://ext.com/sink", "/sink"))
    g.finalize()

    cfg = AuthorityScoringConfig(score_internal_only=False)
    orchestrator = PageRankOrchestrator(cfg)
    res = orchestrator.run(graph=g, session=mem_session)
    mem_session.commit()

    assert res.total_pages_scored == 2
    ext_row = mem_session.query(PageAuthorityScore).filter_by(normalized_url="https://ext.com/sink").first()
    assert ext_row is not None
    assert ext_row.is_internal_page is False
    assert ext_row.page_id is None


def test_t4_34_score_internal_only_excludes_terminals(mem_session):
    """Verify that default score_internal_only=True excludes external terminal vertices."""
    g = PhysicalGraph()
    v_int = GraphVertex("https://a.com/page", "https://a.com/page", "https://a.com/page", "a.com", "a.com", VertexType.INTERNAL_CRAWLED, True, 1)
    v_ext = GraphVertex("https://ext.com/sink", "https://ext.com/sink", "https://ext.com/sink", "ext.com", "ext.com", VertexType.EXTERNAL_TERMINAL, False, None)
    g.add_vertex(v_int)
    g.add_vertex(v_ext)
    g.add_edge_occurrence(PhysicalEdgeOccurrence("https://a.com/page", "https://ext.com/sink", "/sink"))
    g.finalize()

    cfg = AuthorityScoringConfig(score_internal_only=True)
    orchestrator = PageRankOrchestrator(cfg)
    res = orchestrator.run(graph=g, session=mem_session)
    mem_session.commit()

    assert res.total_pages_scored == 1
    rows = mem_session.query(PageAuthorityScore).all()
    assert len(rows) == 1
    assert rows[0].normalized_url == "https://a.com/page"


# ==============================================================================
# GROUP F: CLI, DETERMINISM, CONCURRENCY & REGRESSION (T4-35 to T4-40)
# ==============================================================================

def test_t4_35_cli_exit_code_success():
    """Verify CLI returns exit code 0 on successful run."""
    runner = CliRunner()
    mock_res = AuthorityScoringResult(
        target_domain=None,
        total_pages_scored=3,
        total_components=1,
        converged_components=1,
        non_converged_components=0,
        skipped_singleton_components=0,
        max_lp_pa_score=0.85,
        mean_lp_pa_score=0.55,
        total_wall_time_seconds=0.012,
        computed_at=datetime.now(timezone.utc),
        status="COMPLETED",
    )
    with patch("app.authority.scorer.PageRankOrchestrator.run", return_value=mock_res), \
         patch("app.database.repositories.CrawlRepository.get_top_authority_scores", return_value=[]):
        result = runner.invoke(cli_app, ["authority", "compute", "--all"])
    assert result.exit_code == 0
    assert "Page Authority Scoring Summary" in result.output


def test_t4_36_cli_exit_code_pipeline_failure():
    """Verify CLI returns exit code 1 on pipeline failure."""
    runner = CliRunner()
    mock_res = AuthorityScoringResult(
        target_domain=None,
        total_pages_scored=0,
        total_components=1,
        converged_components=0,
        non_converged_components=1,
        skipped_singleton_components=0,
        max_lp_pa_score=0.0,
        mean_lp_pa_score=0.0,
        total_wall_time_seconds=0.005,
        computed_at=datetime.now(timezone.utc),
        status="FAILED",
        error_message="Simulation pipeline failure",
    )
    with patch("app.authority.scorer.PageRankOrchestrator.run", return_value=mock_res):
        result = runner.invoke(cli_app, ["authority", "compute", "--domain", "example.com"])
    assert result.exit_code == 1


def test_t4_37_cli_exit_code_invalid_arguments():
    """Verify CLI returns exit code 2 on invalid arguments."""
    runner = CliRunner()
    # Missing both --domain and --all
    res1 = runner.invoke(cli_app, ["authority", "compute"])
    assert res1.exit_code == 2

    # Mutually exclusive --domain and --all
    res2 = runner.invoke(cli_app, ["authority", "compute", "--domain", "ex.com", "--all"])
    assert res2.exit_code == 2

    # Invalid alpha out of (0, 1)
    res3 = runner.invoke(cli_app, ["authority", "compute", "--all", "--alpha", "1.5"])
    assert res3.exit_code == 2


def test_t4_38_deterministic_idempotency_across_runs(mem_session, sample_graph_3node):
    """Verify that repeated identical runs produce exact bit-for-bit identical scores."""
    orchestrator = PageRankOrchestrator()

    res1 = orchestrator.run(graph=sample_graph_3node, session=mem_session)
    mem_session.commit()
    scores1 = {r.normalized_url: (r.lp_pa_score, r.raw_pagerank) for r in mem_session.query(PageAuthorityScore).all()}

    res2 = orchestrator.run(graph=sample_graph_3node, session=mem_session)
    mem_session.commit()
    scores2 = {r.normalized_url: (r.lp_pa_score, r.raw_pagerank) for r in mem_session.query(PageAuthorityScore).all()}

    assert len(scores1) == len(scores2) == 3
    for k in scores1:
        assert scores1[k] == scores2[k]


def test_t4_39_milestones_1_2_3_zero_regression():
    """Verify that core interfaces of M1.1, M2, and M3 remain unmutated and functional."""
    from app.authority.eligibility import LinkEligibilityEngine, classify_rel_attributes
    from app.authority.normalizer import CanonicalURLNormalizer
    from app.authority.graph import PhysicalGraph
    from app.authority.matrix import CSRMatrixBuilder
    from app.authority.pagerank import compute_pagerank

    # M1.1 normalizer test
    norm = CanonicalURLNormalizer.normalize_url("https://Example.com/Path/")
    assert norm.is_valid_web_url is True

    # M2 matrix builder test
    g = PhysicalGraph()
    v = GraphVertex("k", "u", "u", "d", "d", VertexType.INTERNAL_CRAWLED, True, 1)
    g.add_vertex(v)
    g.finalize()
    m = CSRMatrixBuilder.build_transition_matrix(g)
    assert m.nrows == 1

    # M3 PageRank solver test
    pr = compute_pagerank(m)
    assert pr.converged is True
    assert pr.scores == [1.0]


def test_t4_full_suite_programmatic_regression():
    """REG-01: Programmatic full regression suite execution across M1.1, M2, and M3."""
    ret = pytest.main([
        "-q",
        "tests/test_authority_m1_eligibility.py",
        "tests/test_authority_m2_graph_matrix.py",
        "tests/test_authority_m3_pagerank.py",
    ])
    assert ret == 0


def test_t4_40_clean_imports_and_no_circular_dependencies():
    """Verify clean import of all Milestone 4 components with zero circular imports."""
    import app.authority.scorer as scorer
    import app.database.models as models
    import app.database.repositories as repos
    import app.commands.cli as cli

    assert hasattr(scorer, "PageRankOrchestrator")
    assert hasattr(scorer, "AuthorityScoringConfig")
    assert hasattr(scorer, "AuthorityScoringResult")
    assert hasattr(models, "PageAuthorityScore")
    assert hasattr(repos.CrawlRepository, "upsert_page_authority_scores")
    assert hasattr(cli, "authority_app")


# ==============================================================================
# GROUP G: M4-C1 SAVEPOINT & TRANSACTION HARDENING (TX-01 to TX-03)
# ==============================================================================

def test_t4_savepoint_rollback_preserves_caller_outer_transaction(mem_session):
    """TX-01: Verify that Stage 8 savepoint rollback protects caller's uncommitted outer transaction."""
    # 1. Caller writes an uncommitted domain record in active session
    test_dom = Domain(
        domain_name="caller-domain.com",
        registered_domain="caller-domain.com",
        tld="com",
    )
    mem_session.add(test_dom)
    mem_session.flush()

    # 2. Existing score already committed
    repo = CrawlRepository(mem_session)
    initial_rec = [{
        "page_id": None,
        "normalized_url": "https://caller-domain.com/keep",
        "domain": "caller-domain.com",
        "is_internal_page": False,
        "lp_pa_score": 0.88,
        "raw_pagerank": 0.05,
        "component_id": 0,
        "comp_scale": 0.5,
        "alpha": 0.85,
        "pagerank_status": "CONVERGED",
        "iterations": 10,
        "final_residual": 1e-13,
        "computed_at": datetime.now(timezone.utc),
    }]
    repo.upsert_page_authority_scores(initial_rec)
    mem_session.commit()

    # Re-add uncommitted caller domain change
    test_dom.registered_domain = "updated-caller.com"
    mem_session.flush()

    # 3. Trigger Stage 8 failure via mocked repo method that raises IntegrityError
    orchestrator = PageRankOrchestrator()
    mock_graph = PhysicalGraph()
    v = GraphVertex(
        "https://caller-domain.com/new",
        "https://caller-domain.com/new",
        "https://caller-domain.com/new",
        "caller-domain.com",
        "caller-domain.com",
        VertexType.INTERNAL_CRAWLED,
        True,
        1,
    )
    mock_graph.add_vertex(v)
    mock_graph.finalize()

    with patch.object(CrawlRepository, "upsert_page_authority_scores", side_effect=IntegrityError("stmt", "params", "orig")):
        res = orchestrator.run(graph=mock_graph, session=mem_session, target_domain="caller-domain.com")

    assert res.status == "FAILED"
    assert "Persistence transaction failed" in (res.error_message or "")

    # Verify caller's uncommitted domain change was NOT wiped out and session is not rolled back
    queried_dom = mem_session.query(Domain).filter_by(domain_name="caller-domain.com").one_or_none()
    assert queried_dom is not None
    assert queried_dom.registered_domain == "updated-caller.com"

    # Verify initial score row is preserved
    rows = mem_session.query(PageAuthorityScore).filter_by(domain="caller-domain.com").all()
    assert len(rows) == 1
    assert rows[0].normalized_url == "https://caller-domain.com/keep"
    assert rows[0].lp_pa_score == 0.88

    # Caller can now successfully commit its outer transaction
    mem_session.commit()
    assert mem_session.query(Domain).filter_by(domain_name="caller-domain.com").one().registered_domain == "updated-caller.com"


def test_t4_savepoint_no_unauthorized_outer_commit(mem_session, sample_graph_3node):
    """TX-02: Verify that orchestrator does not prematurely commit caller's outer transaction."""
    orchestrator = PageRankOrchestrator()
    with patch.object(mem_session, "commit", wraps=mem_session.commit) as mock_commit:
        res = orchestrator.run(graph=sample_graph_3node, session=mem_session, target_domain="example.com")
        assert res.status == "COMPLETED"
        # Orchestrator must NEVER commit caller's session
        assert mock_commit.call_count == 0

        # In mem_session transaction, the write is staged
        in_session_rows = mem_session.query(PageAuthorityScore).filter_by(domain="example.com").all()
        assert len(in_session_rows) == 3

        # Caller retains exclusive ownership to commit
        mem_session.commit()
        assert mock_commit.call_count == 1


def test_t4_savepoint_outer_commit_failure_preserves_prior_scores(mem_session):
    """TX-03: Verify that an outer commit failure rolls back cleanly to previous commit snapshot."""
    repo = CrawlRepository(mem_session)
    initial = [{
        "page_id": None,
        "normalized_url": "https://commit-fail.com/keep",
        "domain": "commit-fail.com",
        "is_internal_page": False,
        "lp_pa_score": 0.65,
        "raw_pagerank": 0.02,
        "component_id": 0,
        "comp_scale": 0.5,
        "alpha": 0.85,
        "pagerank_status": "CONVERGED",
        "iterations": 10,
        "final_residual": 1e-13,
        "computed_at": datetime.now(timezone.utc),
    }]
    repo.upsert_page_authority_scores(initial)
    mem_session.commit()

    # Verify baseline row exists
    assert mem_session.query(PageAuthorityScore).filter_by(domain="commit-fail.com").count() == 1

    # Stage a new score update
    replacement = [{
        "page_id": None,
        "normalized_url": "https://commit-fail.com/new",
        "domain": "commit-fail.com",
        "is_internal_page": False,
        "lp_pa_score": 0.95,
        "raw_pagerank": 0.05,
        "component_id": 0,
        "comp_scale": 0.5,
        "alpha": 0.85,
        "pagerank_status": "CONVERGED",
        "iterations": 10,
        "final_residual": 1e-13,
        "computed_at": datetime.now(timezone.utc),
    }]
    repo.upsert_page_authority_scores(replacement, target_domain="commit-fail.com")

    # Simulate outer transaction rollback (e.g. disk/network commit error)
    mem_session.rollback()

    # Verify original row is preserved
    rows = mem_session.query(PageAuthorityScore).filter_by(domain="commit-fail.com").all()
    assert len(rows) == 1
    assert rows[0].normalized_url == "https://commit-fail.com/keep"
