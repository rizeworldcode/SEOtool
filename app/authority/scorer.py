"""
LinkPilot AI — Phase 1B Milestone 4: Page Authority Scoring & Persistence Engine.
High-performance pipeline orchestrating graph extraction, WCC decomposition,
CSR matrix construction, PageRank computation, LP-PA scoring, and atomic persistence.
Zero external package dependencies beyond LinkPilot standard stack.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from loguru import logger
from sqlalchemy.orm import Session

from app.authority.graph import (
    GraphVertex,
    PhysicalGraph,
    VertexType,
    WeaklyConnectedComponent,
    build_observed_graph_from_db,
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
from app.authority.domain_scorer import (
    DEFAULT_DA_ALPHA,
    DEFAULT_DA_BETA,
    DEFAULT_DA_EPSILON,
    DEFAULT_DA_GAMMA,
    DEFAULT_DA_REF_N,
    DEFAULT_DA_TOP_K,
    aggregate_domain_authority_from_page_records,
    validate_da_parameters,
)
from app.database.connection import get_db
from app.database.repositories import CrawlRepository


# ==============================================================================
# CONFIGURATION & RESULT DATA CONTRACTS
# ==============================================================================

@dataclass
class AuthorityScoringConfig:
    """Configuration options for the Page & Domain Authority scoring engine."""
    alpha: float = 0.85
    tol: float = 1e-12
    max_iter: int = 200
    use_acceleration: bool = False
    score_internal_only: bool = True
    per_component: bool = True
    min_component_size: int = 1
    compute_da: bool = False
    da_alpha: float = DEFAULT_DA_ALPHA
    da_beta: float = DEFAULT_DA_BETA
    da_gamma: float = DEFAULT_DA_GAMMA
    da_top_k: int = DEFAULT_DA_TOP_K
    da_epsilon: float = DEFAULT_DA_EPSILON
    da_reference_page_count: float = DEFAULT_DA_REF_N

    def __post_init__(self):
        if not (0.0 < self.alpha < 1.0):
            raise ValueError(f"Damping factor alpha must be in (0, 1), got {self.alpha}")
        if self.tol <= 0.0:
            raise ValueError(f"Tolerance tol must be positive, got {self.tol}")
        if self.max_iter <= 0:
            raise ValueError(f"max_iter must be positive, got {self.max_iter}")
        if self.min_component_size < 1:
            raise ValueError(f"min_component_size must be >= 1, got {self.min_component_size}")
        validate_da_parameters(
            alpha=self.da_alpha,
            beta=self.da_beta,
            gamma=self.da_gamma,
            top_k=self.da_top_k,
            epsilon=self.da_epsilon,
            reference_page_count=self.da_reference_page_count,
        )


@dataclass
class AuthorityScoringResult:
    """Execution telemetry and outcome of an authority scoring run."""
    target_domain: Optional[str]
    total_pages_scored: int
    total_components: int
    converged_components: int
    non_converged_components: int
    skipped_singleton_components: int
    max_lp_pa_score: float
    mean_lp_pa_score: float
    total_wall_time_seconds: float
    computed_at: datetime
    status: str  # "COMPLETED" or "FAILED"
    component_results: List[Dict[str, Any]] = field(default_factory=list)
    error_message: Optional[str] = None
    total_domains_scored: int = 0
    max_lp_da_score: float = 0.0
    mean_lp_da_score: float = 0.0
    domain_results: List[Dict[str, Any]] = field(default_factory=list)


# ==============================================================================
# PAGE RANK ORCHESTRATOR ENGINE
# ==============================================================================

class PageRankOrchestrator:
    """
    Orchestrates the 9-stage Page Authority Scoring pipeline:
      Stage 1: Initialization & validation
      Stage 2: Graph extraction (DB or in-memory)
      Stage 3: Weakly Connected Component (WCC) decomposition
      Stage 4: PageRank solving per component (or globally)
      Stage 5: LP-PA score mapping with component significance scaling
      Stage 6: Pre-persistence batch validation gate
      Stage 7: Transaction boundary management
      Stage 8: Atomic scope deletion and bulk insertion
      Stage 9: Telemetry assembly and reporting
    """

    def __init__(self, config: Optional[AuthorityScoringConfig] = None):
        self.config = config or AuthorityScoringConfig()

    def run(
        self,
        db_path: Optional[str] = None,
        graph: Optional[PhysicalGraph] = None,
        target_domain: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> AuthorityScoringResult:
        """
        Execute the full authority scoring pipeline.
        
        Args:
            db_path: Path to SQLite database if loading graph from DB.
            graph: Optional pre-constructed PhysicalGraph (for testing/in-memory).
            target_domain: Optional domain to scope scoring and persistence.
            session: Optional active SQLAlchemy session (e.g. for testing).
        """
        start_time = time.perf_counter()
        now = datetime.now(timezone.utc)

        # ----------------------------------------------------------------------
        # Stage 1 & 2: Extract Graph
        # ----------------------------------------------------------------------
        try:
            if graph is None:
                path = db_path or "./data/linkpilot.db"
                working_graph = build_observed_graph_from_db(path, require_read_only=True)
            else:
                working_graph = graph

            # Optional Domain Filtering
            if target_domain is not None:
                working_graph = self._filter_graph_by_domain(working_graph, target_domain)

        except Exception as e:
            logger.error(f"Stage 2 (Graph Extraction) failed: {e}")
            return AuthorityScoringResult(
                target_domain=target_domain,
                total_pages_scored=0,
                total_components=0,
                converged_components=0,
                non_converged_components=0,
                skipped_singleton_components=0,
                max_lp_pa_score=0.0,
                mean_lp_pa_score=0.0,
                total_wall_time_seconds=time.perf_counter() - start_time,
                computed_at=now,
                status="FAILED",
                error_message=f"Graph extraction failed: {e}",
            )

        n_vertices = len(working_graph.vertices)
        if n_vertices == 0:
            logger.info("Empty graph provided: 0 pages scored.")
            return AuthorityScoringResult(
                target_domain=target_domain,
                total_pages_scored=0,
                total_components=0,
                converged_components=0,
                non_converged_components=0,
                skipped_singleton_components=0,
                max_lp_pa_score=0.0,
                mean_lp_pa_score=0.0,
                total_wall_time_seconds=time.perf_counter() - start_time,
                computed_at=now,
                status="COMPLETED",
            )

        # ----------------------------------------------------------------------
        # Stage 3: WCC Decomposition
        # ----------------------------------------------------------------------
        if not working_graph._finalized:
            working_graph.finalize()

        components = working_graph.find_weakly_connected_components()
        total_components = len(components)
        converged_count = 0
        non_converged_count = 0
        singleton_count = 0
        component_diagnostics: List[Dict[str, Any]] = []

        all_score_records: List[Dict[str, Any]] = []

        # ----------------------------------------------------------------------
        # Stage 4 & 5: PageRank Solving & LP-PA Score Mapping
        # ----------------------------------------------------------------------
        try:
            if self.config.per_component:
                for comp in components:
                    comp_size = comp.node_count
                    if comp_size == 1:
                        singleton_count += 1

                    # Assemble CSR transition matrix for this component
                    matrix = CSRMatrixBuilder.build_transition_matrix(
                        working_graph, component=comp, exclude_self_loops=True
                    )

                    # Solve PageRank
                    pr_result = compute_pagerank(
                        matrix=matrix,
                        alpha=self.config.alpha,
                        tol=self.config.tol,
                        max_iter=self.config.max_iter,
                        use_acceleration=self.config.use_acceleration,
                    )

                    # Check for numerical overflow / NaN
                    if pr_result.status == PageRankStatus.NUMERICAL_OVERFLOW_OR_NAN:
                        logger.warning(
                            f"Component {comp.component_id} (size {comp_size}) encountered "
                            f"NUMERICAL_OVERFLOW_OR_NAN: skipping component."
                        )
                        non_converged_count += 1
                        component_diagnostics.append({
                            "component_id": comp.component_id,
                            "node_count": comp_size,
                            "status": pr_result.status.value,
                            "skipped": True,
                        })
                        continue

                    if pr_result.converged:
                        converged_count += 1
                    else:
                        non_converged_count += 1

                    component_diagnostics.append({
                        "component_id": comp.component_id,
                        "node_count": comp_size,
                        "status": pr_result.status.value,
                        "iterations": pr_result.iterations,
                        "final_residual": pr_result.final_residual,
                        "skipped": False,
                    })

                    # Map raw PageRank stationary probabilities to LP-PA scores
                    lp_pa_map = compute_page_authority_scores(
                        matrix=matrix,
                        stationary_dist=pr_result.scores,
                        comp_scale=comp.comp_scale,
                        alpha=self.config.alpha,
                    )

                    # Assemble score dictionaries for component vertices
                    for idx, key in enumerate(matrix.node_keys):
                        vertex = working_graph.vertices[key]
                        is_internal = vertex.is_authoritative_scored_page

                        if self.config.score_internal_only and not is_internal:
                            continue

                        rec = {
                            "page_id": vertex.page_id if is_internal else None,
                            "normalized_url": vertex.normalized_url or vertex.key,
                            "domain": vertex.domain,
                            "is_internal_page": is_internal,
                            "lp_pa_score": lp_pa_map[key],
                            "raw_pagerank": pr_result.scores[idx],
                            "component_id": comp.component_id,
                            "comp_scale": comp.comp_scale,
                            "alpha": self.config.alpha,
                            "pagerank_status": pr_result.status.value,
                            "iterations": pr_result.iterations,
                            "final_residual": pr_result.final_residual,
                            "computed_at": now,
                        }
                        all_score_records.append(rec)

            else:
                # Global Monolithic Mode (per_component = False)
                matrix = CSRMatrixBuilder.build_transition_matrix(
                    working_graph, component=None, exclude_self_loops=True
                )
                pr_result = compute_pagerank(
                    matrix=matrix,
                    alpha=self.config.alpha,
                    tol=self.config.tol,
                    max_iter=self.config.max_iter,
                    use_acceleration=self.config.use_acceleration,
                )

                if pr_result.status == PageRankStatus.NUMERICAL_OVERFLOW_OR_NAN:
                    logger.error("Global PageRank encountered NUMERICAL_OVERFLOW_OR_NAN.")
                    return AuthorityScoringResult(
                        target_domain=target_domain,
                        total_pages_scored=0,
                        total_components=total_components,
                        converged_components=0,
                        non_converged_components=1,
                        skipped_singleton_components=0,
                        max_lp_pa_score=0.0,
                        mean_lp_pa_score=0.0,
                        total_wall_time_seconds=time.perf_counter() - start_time,
                        computed_at=now,
                        status="FAILED",
                        error_message="Global solver encountered NaN/overflow.",
                    )

                if pr_result.converged:
                    converged_count = 1
                else:
                    non_converged_count = 1

                global_comp_scale = compute_component_scale(n_vertices)
                lp_pa_map = compute_page_authority_scores(
                    matrix=matrix,
                    stationary_dist=pr_result.scores,
                    comp_scale=global_comp_scale,
                    alpha=self.config.alpha,
                )

                for idx, key in enumerate(matrix.node_keys):
                    vertex = working_graph.vertices[key]
                    is_internal = vertex.is_authoritative_scored_page

                    if self.config.score_internal_only and not is_internal:
                        continue

                    rec = {
                        "page_id": vertex.page_id if is_internal else None,
                        "normalized_url": vertex.normalized_url or vertex.key,
                        "domain": vertex.domain,
                        "is_internal_page": is_internal,
                        "lp_pa_score": lp_pa_map[key],
                        "raw_pagerank": pr_result.scores[idx],
                        "component_id": 0,
                        "comp_scale": global_comp_scale,
                        "alpha": self.config.alpha,
                        "pagerank_status": pr_result.status.value,
                        "iterations": pr_result.iterations,
                        "final_residual": pr_result.final_residual,
                        "computed_at": now,
                    }
                    all_score_records.append(rec)

        except Exception as e:
            logger.error(f"Stage 4/5 (Solving / Score Mapping) failed: {e}")
            return AuthorityScoringResult(
                target_domain=target_domain,
                total_pages_scored=0,
                total_components=total_components,
                converged_components=converged_count,
                non_converged_components=non_converged_count,
                skipped_singleton_components=singleton_count,
                max_lp_pa_score=0.0,
                mean_lp_pa_score=0.0,
                total_wall_time_seconds=time.perf_counter() - start_time,
                computed_at=now,
                status="FAILED",
                error_message=f"Computation failed: {e}",
            )

        # ----------------------------------------------------------------------
        # Stage 6A: Optional Domain Authority (LP-DA) In-Memory Aggregation
        # ----------------------------------------------------------------------
        all_da_records: List[Dict[str, Any]] = []
        if self.config.compute_da:
            try:
                all_da_records = self._aggregate_domain_authority_scores(
                    all_score_records, computed_at=now
                )
            except Exception as e:
                logger.error(f"Stage 6A (Domain Authority Aggregation) failed: {e}")
                return AuthorityScoringResult(
                    target_domain=target_domain,
                    total_pages_scored=0,
                    total_components=total_components,
                    converged_components=converged_count,
                    non_converged_components=non_converged_count,
                    skipped_singleton_components=singleton_count,
                    max_lp_pa_score=0.0,
                    mean_lp_pa_score=0.0,
                    total_wall_time_seconds=time.perf_counter() - start_time,
                    computed_at=now,
                    status="FAILED",
                    error_message=f"Domain Authority aggregation failed: {e}",
                )

        # ----------------------------------------------------------------------
        # Stage 6B: Pre-Persistence Batch Validation Gate (PA + DA)
        # ----------------------------------------------------------------------
        validation_passed, validation_err = self._validate_batch(all_score_records, n_vertices)
        if validation_passed and self.config.compute_da:
            validation_passed, validation_err = self._validate_da_batch(
                all_da_records, len(all_score_records)
            )
        if not validation_passed:
            logger.critical(f"Stage 6 Validation Gate REJECTED batch: {validation_err}")
            return AuthorityScoringResult(
                target_domain=target_domain,
                total_pages_scored=0,
                total_components=total_components,
                converged_components=converged_count,
                non_converged_components=non_converged_count,
                skipped_singleton_components=singleton_count,
                max_lp_pa_score=0.0,
                mean_lp_pa_score=0.0,
                total_wall_time_seconds=time.perf_counter() - start_time,
                computed_at=now,
                status="FAILED",
                error_message=f"Pre-persistence validation gate failed: {validation_err}",
            )

        # ----------------------------------------------------------------------
        # Stage 7 & 8: Atomic Persistence Transaction (Shared SAVEPOINT)
        # ----------------------------------------------------------------------
        try:
            if session is not None:
                # Wrap caller-provided session in SAVEPOINT to isolate persistence failure
                with session.begin_nested():
                    repo = CrawlRepository(session)
                    repo.upsert_page_authority_scores(all_score_records, target_domain=target_domain)
                    if self.config.compute_da:
                        repo.upsert_domain_authority_scores(all_da_records, target_domain=target_domain)
            else:
                with get_db() as sess:
                    repo = CrawlRepository(sess)
                    repo.upsert_page_authority_scores(all_score_records, target_domain=target_domain)
                    if self.config.compute_da:
                        repo.upsert_domain_authority_scores(all_da_records, target_domain=target_domain)
        except Exception as e:
            logger.error(f"Stage 8 (Database Persistence) failed and was rolled back: {e}")
            return AuthorityScoringResult(
                target_domain=target_domain,
                total_pages_scored=0,
                total_components=total_components,
                converged_components=converged_count,
                non_converged_components=non_converged_count,
                skipped_singleton_components=singleton_count,
                max_lp_pa_score=0.0,
                mean_lp_pa_score=0.0,
                total_wall_time_seconds=time.perf_counter() - start_time,
                computed_at=now,
                status="FAILED",
                error_message=f"Persistence transaction failed: {e}",
            )

        # ----------------------------------------------------------------------
        # Stage 9: Assembly & Telemetry
        # ----------------------------------------------------------------------
        scores_list = [r["lp_pa_score"] for r in all_score_records]
        max_score = max(scores_list, default=0.0)
        mean_score = (sum(scores_list) / len(scores_list)) if scores_list else 0.0

        da_scores_list = [r["lp_da_score"] for r in all_da_records]
        max_da_score = max(da_scores_list, default=0.0)
        mean_da_score = (sum(da_scores_list) / len(da_scores_list)) if da_scores_list else 0.0

        return AuthorityScoringResult(
            target_domain=target_domain,
            total_pages_scored=len(all_score_records),
            total_components=total_components,
            converged_components=converged_count,
            non_converged_components=non_converged_count,
            skipped_singleton_components=singleton_count,
            max_lp_pa_score=max_score,
            mean_lp_pa_score=mean_score,
            total_wall_time_seconds=time.perf_counter() - start_time,
            computed_at=now,
            status="COMPLETED",
            component_results=component_diagnostics,
            total_domains_scored=len(all_da_records),
            max_lp_da_score=max_da_score,
            mean_lp_da_score=mean_da_score,
            domain_results=all_da_records,
        )

    # --------------------------------------------------------------------------
    # HELPER METHODS
    # --------------------------------------------------------------------------

    def _aggregate_domain_authority_scores(
        self,
        pa_records: List[Dict[str, Any]],
        computed_at: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Aggregate in-memory Page Authority records into DomainAuthorityScore payloads."""
        computations = aggregate_domain_authority_from_page_records(
            page_records=pa_records,
            alpha=self.config.da_alpha,
            beta=self.config.da_beta,
            gamma=self.config.da_gamma,
            top_k=self.config.da_top_k,
            epsilon=self.config.da_epsilon,
            reference_page_count=self.config.da_reference_page_count,
            score_internal_only=self.config.score_internal_only,
            computed_at=computed_at,
        )
        return [comp.to_record_dict(computed_at=computed_at) for comp in computations]

    def _filter_graph_by_domain(self, graph: PhysicalGraph, target_domain: str) -> PhysicalGraph:
        """Construct an induced subgraph containing only vertices matching target_domain."""
        filtered = PhysicalGraph()
        for k, v in graph.vertices.items():
            if v.domain == target_domain:
                filtered.add_vertex(v)

        # Copy edge occurrences if available
        if hasattr(graph, "_raw_occurrences") and graph._raw_occurrences:
            for occ in graph._raw_occurrences:
                if occ.source_key in filtered.vertices and occ.target_key in filtered.vertices:
                    filtered.add_edge_occurrence(occ)
            filtered.finalize()
        else:
            # Replicate direct edges
            filtered.finalize()
            for src, t_dict in graph.edges.items():
                if src in filtered.vertices:
                    for tgt, edge in t_dict.items():
                        if tgt in filtered.vertices:
                            if src not in filtered.edges:
                                filtered.edges[src] = {}
                            filtered.edges[src][tgt] = edge

        return filtered

    def _validate_batch(
        self, records: List[Dict[str, Any]], n_graph_vertices: int
    ) -> Tuple[bool, Optional[str]]:
        """
        Stage 6 Gate: Validates payload before opening database transaction.
        Checks bounds, key uniqueness, non-null requirements, and referential constraints.
        """
        if n_graph_vertices > 0 and not records:
            return False, "Graph contained vertices but 0 valid scores were generated."

        seen_urls: Set[str] = set()

        for idx, r in enumerate(records):
            url = r.get("normalized_url")
            if not url or not isinstance(url, str):
                return False, f"Record {idx} has invalid or empty normalized_url: {url}"
            if url in seen_urls:
                return False, f"Record {idx} contains duplicate normalized_url: {url}"
            seen_urls.add(url)

            score = r.get("lp_pa_score")
            if score is None or not math.isfinite(score) or not (0.0 <= score <= 1.0):
                return False, f"Record {idx} ({url}) has out-of-bounds lp_pa_score: {score}"

            pr = r.get("raw_pagerank")
            if pr is None or not math.isfinite(pr) or pr <= 0.0:
                return False, f"Record {idx} ({url}) has invalid raw_pagerank: {pr}"

            if r.get("is_internal_page"):
                pid = r.get("page_id")
                if pid is None or not isinstance(pid, int) or pid <= 0:
                    return False, f"Record {idx} ({url}) marked internal but has invalid page_id: {pid}"

        return True, None

    def _validate_da_batch(
        self, da_records: List[Dict[str, Any]], n_scored_pages: int
    ) -> Tuple[bool, Optional[str]]:
        """
        Stage 6B Gate: Validates Domain Authority records before opening database transaction.
        """
        if n_scored_pages > 0 and not da_records:
            return False, "Scored pages were present but 0 Domain Authority records were generated."

        seen_domains: Set[str] = set()
        for idx, r in enumerate(da_records):
            dom = r.get("domain_name")
            if not dom or not isinstance(dom, str) or not dom.strip():
                return False, f"DA Record {idx} has invalid or empty domain_name: {dom!r}"
            if dom in seen_domains:
                return False, f"DA Record {idx} contains duplicate domain_name: {dom}"
            seen_domains.add(dom)

            for field_name in ("lp_da_score", "anchor_page_pa", "top_k_harmonic_pa", "mean_page_pa", "breadth_score"):
                val = r.get(field_name)
                if val is None or isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(float(val)) or not (0.0 <= float(val) <= 1.0):
                    return False, f"DA Record {idx} ({dom}) has out-of-bounds {field_name}: {val!r}"

            disp = r.get("lp_da_display")
            if disp is None or isinstance(disp, bool) or not isinstance(disp, (int, float)) or not math.isfinite(float(disp)) or not (0.0 <= float(disp) <= 100.0):
                return False, f"DA Record {idx} ({dom}) has out-of-bounds lp_da_display: {disp!r}"

            cnt = r.get("scored_pages_count")
            if cnt is None or isinstance(cnt, bool) or not isinstance(cnt, int) or cnt < 0:
                return False, f"DA Record {idx} ({dom}) has invalid scored_pages_count: {cnt!r}"

        return True, None


# ==============================================================================
# CONVENIENCE ENTRYPOINT
# ==============================================================================

def compute_and_persist_authority_scores(
    db_path: str = "./data/linkpilot.db",
    target_domain: Optional[str] = None,
    alpha: float = 0.85,
    tol: float = 1e-12,
    max_iter: int = 200,
    use_acceleration: bool = False,
    compute_da: bool = False,
    session: Optional[Session] = None,
) -> AuthorityScoringResult:
    """Convenience function executing the complete Page (and optional Domain) Authority pipeline."""
    config = AuthorityScoringConfig(
        alpha=alpha,
        tol=tol,
        max_iter=max_iter,
        use_acceleration=use_acceleration,
        compute_da=compute_da,
    )
    orchestrator = PageRankOrchestrator(config=config)
    return orchestrator.run(db_path=db_path, target_domain=target_domain, session=session)
