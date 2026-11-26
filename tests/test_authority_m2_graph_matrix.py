"""
LinkPilot AI — Phase 1B Milestone 2: Test Suite
Comprehensive verification of PhysicalGraph, WCC decomposition, CSR transition matrix,
matrix transpose orientation, PageRank convergence, memory bounds, and defensive port parsing.
"""

import math
import os
import sqlite3
import pytest

from app.authority.normalizer import (
    CanonicalURLNormalizer,
    SPARouteTelemetry,
)
from app.authority.graph import (
    PhysicalGraph,
    GraphVertex,
    PhysicalEdgeOccurrence,
    VertexType,
    compute_rel_multiplier,
    compute_dom_multiplier,
    compute_occurrence_quality,
    compute_component_scale,
    build_observed_graph_from_db,
)
from app.authority.matrix import (
    CSRMatrix,
    CSRMatrixBuilder,
    identify_dangling_nodes,
    compute_stationary_distribution,
    compute_page_authority_scores,
    calculate_expected_csr_memory,
)


# ==============================================================================
# 1. NORMALIZER DEFENSIVE PORT & HOSTNAME TESTS (ADR-09)
# ==============================================================================

def test_normalizer_port_parsing_valueerror_safety():
    """Verify that non-numeric port strings do not crash normalizer and return invalid."""
    res = CanonicalURLNormalizer.normalize_url("http://example.com:notanumber/")
    assert res.is_valid_web_url is False
    assert "Invalid port" in (res.error_message or "")


def test_normalizer_port_out_of_range_safety():
    """Verify ports outside 0-65535 are rejected gracefully."""
    res = CanonicalURLNormalizer.normalize_url("http://example.com:70000/")
    assert res.is_valid_web_url is False
    assert "Invalid port" in (res.error_message or "")


def test_normalizer_empty_hostname_with_port():
    """Verify URL with port but no hostname is marked invalid."""
    res = CanonicalURLNormalizer.normalize_url("http://:80/")
    assert res.is_valid_web_url is False


def test_normalizer_ipv6_zone_id_rejected():
    """Verify IPv6 scoped zone IDs are rejected."""
    res = CanonicalURLNormalizer.normalize_url("http://[fe80::1%eth0]/")
    assert res.is_valid_web_url is False


# ==============================================================================
# 2. EDGE QUALITY & MULTIPLIER TESTS (ADR-05)
# ==============================================================================

def test_rel_multipliers():
    """Verify rel attribute quality multiplier mappings."""
    assert compute_rel_multiplier(None) == 1.0
    assert compute_rel_multiplier("") == 1.0
    assert compute_rel_multiplier("follow") == 1.0
    assert compute_rel_multiplier("ugc") == 0.5
    assert compute_rel_multiplier("nofollow") == 0.0
    assert compute_rel_multiplier("sponsored") == 0.0
    assert compute_rel_multiplier("nofollow, noopener") == 0.0
    assert compute_rel_multiplier("ugc, sponsored") == 0.0  # sponsored overrides


def test_dom_context_multipliers():
    """Verify DOM context multiplier mappings."""
    assert compute_dom_multiplier("main_content") == 1.0
    assert compute_dom_multiplier("article") == 1.0
    assert compute_dom_multiplier("body") == 1.0
    assert compute_dom_multiplier("header") == 0.8
    assert compute_dom_multiplier("nav") == 0.8
    assert compute_dom_multiplier("sidebar") == 0.6
    assert compute_dom_multiplier("footer") == 0.4
    assert compute_dom_multiplier("random_div") == 0.7  # unknown


def test_occurrence_averaged_quality_and_diminishing_returns():
    """Verify aggregation of multiple occurrences between same source and target."""
    g = PhysicalGraph()
    src = "https://example.com/source"
    tgt = "https://example.com/target"

    g.add_vertex(GraphVertex(
        key=src, raw_url=src, normalized_url=src, domain="example.com",
        site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
        is_authoritative_scored_page=True
    ))
    g.add_vertex(GraphVertex(
        key=tgt, raw_url=tgt, normalized_url=tgt, domain="example.com",
        site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
        is_authoritative_scored_page=True
    ))

    # Add 2 occurrences: header (0.8) and footer (0.4)
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key=src, target_key=tgt, raw_href=tgt, dom_context="header"))
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key=src, target_key=tgt, raw_href=tgt, dom_context="footer"))
    g.finalize()

    edge = g.edges[src][tgt]
    assert edge.occurrence_count == 2
    # Mean quality = (0.8 + 0.4) / 2 = 0.6
    assert abs(edge.quality_factor - 0.6) < 1e-6

    # Weight = Q * (1 + ln(1 + 0.1 * 1)) = 0.6 * (1 + ln(1.1))
    expected_weight = 0.6 * (1.0 + math.log(1.1))
    assert abs(edge.weight - expected_weight) < 1e-6


# ==============================================================================
# 3. VERTEX PARTITIONING & SELF-LOOP ISOLATION (ADR-03, ADR-06, ADR-07)
# ==============================================================================

def test_vertex_partitioning_and_unscored_contract():
    """Verify internal crawled vs external terminal vs SPA unscored vertices."""
    v_internal = GraphVertex(
        key="https://example.com/a", raw_url="https://example.com/a", normalized_url="https://example.com/a",
        domain="example.com", site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
        is_authoritative_scored_page=True
    )
    v_external = GraphVertex(
        key="https://other.com/b", raw_url="https://other.com/b", normalized_url="https://other.com/b",
        domain="other.com", site_boundary="other.com", vertex_type=VertexType.EXTERNAL_TERMINAL,
        is_authoritative_scored_page=False
    )
    v_spa = GraphVertex(
        key="https://example.com/#/app", raw_url="https://example.com/#/app", normalized_url="https://example.com/#/app",
        domain="example.com", site_boundary="example.com", vertex_type=VertexType.SPA_UNSCORED,
        is_authoritative_scored_page=False
    )

    assert v_internal.is_authoritative_scored_page is True
    assert v_external.is_authoritative_scored_page is False
    assert v_spa.is_authoritative_scored_page is False


def test_self_loop_isolation_in_graph_and_matrix():
    """Verify self-loops are retained in provenance but zeroed in transition matrix."""
    g = PhysicalGraph()
    k1 = "https://example.com/page1"
    k2 = "https://example.com/page2"

    for k in (k1, k2):
        g.add_vertex(GraphVertex(
            key=k, raw_url=k, normalized_url=k, domain="example.com",
            site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
            is_authoritative_scored_page=True
        ))

    # Self-loop on page 1 + directed edge from page 1 to page 2
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key=k1, target_key=k1, raw_href=k1, dom_context="body"))
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key=k1, target_key=k2, raw_href=k2, dom_context="body"))
    g.finalize()

    assert len(g.self_loops) == 1
    assert g.self_loops[0].source_key == k1

    # In transition matrix, self-loop should be excluded
    M = CSRMatrixBuilder.build_transition_matrix(g, exclude_self_loops=True)
    dense = M.to_dense()

    idx1 = M.key_to_idx[k1]
    idx2 = M.key_to_idx[k2]

    # Row 1 diagonal should be 0.0, and 1 -> 2 should be 1.0
    assert dense[idx1][idx1] == 0.0
    assert dense[idx1][idx2] == 1.0


# ==============================================================================
# 4. WCC DECOMPOSITION & COMPONENT SCALING (ADR-10)
# ==============================================================================

def test_wcc_decomposition_on_disconnected_clusters():
    """Verify WCC correctly identifies disconnected clusters and sorts by size."""
    g = PhysicalGraph()

    # Cluster A: nodes 1, 2, 3
    for i in (1, 2, 3):
        k = f"https://example.com/{i}"
        g.add_vertex(GraphVertex(
            key=k, raw_url=k, normalized_url=k, domain="example.com",
            site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
            is_authoritative_scored_page=True
        ))
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key="https://example.com/1", target_key="https://example.com/2", raw_href=""))
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key="https://example.com/2", target_key="https://example.com/3", raw_href=""))

    # Cluster B: nodes 4, 5
    for i in (4, 5):
        k = f"https://example.com/{i}"
        g.add_vertex(GraphVertex(
            key=k, raw_url=k, normalized_url=k, domain="example.com",
            site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
            is_authoritative_scored_page=True
        ))
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key="https://example.com/4", target_key="https://example.com/5", raw_href=""))

    # Cluster C: isolated node 6
    k6 = "https://example.com/6"
    g.add_vertex(GraphVertex(
        key=k6, raw_url=k6, normalized_url=k6, domain="example.com",
        site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
        is_authoritative_scored_page=True
    ))

    g.finalize()
    wccs = g.find_weakly_connected_components()

    assert len(wccs) == 3
    assert wccs[0].node_count == 3
    assert wccs[1].node_count == 2
    assert wccs[2].node_count == 1


def test_component_scale_calculation():
    """Verify CompScale values match Version 1.2.2 Addendum definition."""
    # N = 100 with N_ref = 100 -> CompScale = ln(101) / (2 * ln(101)) = 0.500
    scale_100 = compute_component_scale(100, n_ref=100.0)
    assert abs(scale_100 - 0.500) < 1e-6

    # N = 1
    scale_1 = compute_component_scale(1, n_ref=100.0)
    expected_1 = math.log(2.0) / (math.log(2.0) + math.log(101.0))
    assert abs(scale_1 - expected_1) < 1e-6
    assert 0.0 < scale_1 < 0.2

    # Monotonicity
    assert compute_component_scale(10) < compute_component_scale(100) < compute_component_scale(1000)


# ==============================================================================
# 5. CSR MATRIX, TRANSPOSE & MEMORY (ADR-01, ADR-02)
# ==============================================================================

def test_csr_memory_calculation_and_byte_bounds():
    """Verify CSR exact memory formula against array buffers."""
    n_nodes = 100
    n_edges = 500
    mem_calc = calculate_expected_csr_memory(n_nodes, n_edges)

    expected_csr = 12 * 500 + 4 * 100 + 4  # 6404 bytes
    assert mem_calc["csr_bytes"] == expected_csr
    assert mem_calc["peak_assembly_bytes"] == 28 * 500 + 4 * 100 + 4


def test_csr_row_stochasticity_property():
    """Verify non-dangling rows sum to 1.0 in CSR transition matrix."""
    g = PhysicalGraph()
    for i in range(4):
        k = f"https://example.com/p{i}"
        g.add_vertex(GraphVertex(
            key=k, raw_url=k, normalized_url=k, domain="example.com",
            site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
            is_authoritative_scored_page=True
        ))

    # Node 0 links to 1, 2, 3 with different contexts
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key="https://example.com/p0", target_key="https://example.com/p1", raw_href="", dom_context="body"))
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key="https://example.com/p0", target_key="https://example.com/p2", raw_href="", dom_context="header"))
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key="https://example.com/p0", target_key="https://example.com/p3", raw_href="", dom_context="footer"))
    g.finalize()

    M = CSRMatrixBuilder.build_transition_matrix(g)
    dense = M.to_dense()

    # Row 0 sum must equal 1.0
    assert abs(sum(dense[0]) - 1.0) < 1e-9

    # Rows 1, 2, 3 are dangling (out-degree 0), sum is 0.0
    for r in (1, 2, 3):
        assert sum(dense[r]) == 0.0


def test_csr_matrix_transpose_identity_and_matvec():
    """Verify M^T matvec matches M matvec_transpose identically with 0 error."""
    g = PhysicalGraph()
    for i in range(5):
        k = f"https://example.com/node{i}"
        g.add_vertex(GraphVertex(
            key=k, raw_url=k, normalized_url=k, domain="example.com",
            site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
            is_authoritative_scored_page=True
        ))

    edges = [(0, 1), (0, 2), (1, 3), (2, 3), (3, 4), (4, 0)]
    for src_i, tgt_i in edges:
        g.add_edge_occurrence(PhysicalEdgeOccurrence(
            source_key=f"https://example.com/node{src_i}",
            target_key=f"https://example.com/node{tgt_i}",
            raw_href=""
        ))
    g.finalize()

    M = CSRMatrixBuilder.build_transition_matrix(g)
    MT = M.transpose()

    # Test on an arbitrary probability vector
    x = [0.1, 0.2, 0.15, 0.25, 0.3]
    y_from_transpose_matrix = MT.matvec(x)
    y_from_matvec_transpose = M.matvec_transpose(x)

    for a, b in zip(y_from_transpose_matrix, y_from_matvec_transpose):
        assert abs(a - b) < 1e-12


# ==============================================================================
# 6. PAGERANK 3-NODE BENCHMARK & STATIONARY CONVERGENCE (ADR-01)
# ==============================================================================

def test_pagerank_3node_mathematical_example():
    """
    Verify exact PageRank stationary probability vector against the 3-node
    analytical calculation in MILESTONE_2_MATHEMATICAL_VALIDATION.md:
    Graph: 1 -> 2, 1 -> 3, 2 -> 3, 3 -> 1.
    Expected stationary distribution with alpha = 0.85:
      p1 ≈ 0.3878, p2 ≈ 0.2148, p3 ≈ 0.3974.
    """
    g = PhysicalGraph()
    k1, k2, k3 = "https://example.com/1", "https://example.com/2", "https://example.com/3"

    for k in (k1, k2, k3):
        g.add_vertex(GraphVertex(
            key=k, raw_url=k, normalized_url=k, domain="example.com",
            site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
            is_authoritative_scored_page=True
        ))

    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key=k1, target_key=k2, raw_href=""))
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key=k1, target_key=k3, raw_href=""))
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key=k2, target_key=k3, raw_href=""))
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key=k3, target_key=k1, raw_href=""))
    g.finalize()

    M = CSRMatrixBuilder.build_transition_matrix(g)
    p = compute_stationary_distribution(M, alpha=0.85, max_iter=200, tol=1e-14)

    assert abs(sum(p) - 1.0) < 1e-10

    idx1 = M.key_to_idx[k1]
    idx2 = M.key_to_idx[k2]
    idx3 = M.key_to_idx[k3]

    # Compare with analytical derivation
    assert abs(p[idx1] - 0.387789) < 1e-4
    assert abs(p[idx2] - 0.214810) < 1e-4
    assert abs(p[idx3] - 0.397401) < 1e-4

    # Node 3 must have higher authority than Node 1 and Node 2
    assert p[idx3] > p[idx1] > p[idx2]


def test_page_authority_score_mapping_bounds():
    """Verify that mapped Page Authority scores lie strictly in [0.0, 1.0]."""
    g = PhysicalGraph()
    for i in range(10):
        k = f"https://example.com/page{i}"
        g.add_vertex(GraphVertex(
            key=k, raw_url=k, normalized_url=k, domain="example.com",
            site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
            is_authoritative_scored_page=True
        ))
        if i > 0:
            g.add_edge_occurrence(PhysicalEdgeOccurrence(
                source_key=f"https://example.com/page{i-1}",
                target_key=f"https://example.com/page{i}",
                raw_href=""
            ))
    g.finalize()

    M = CSRMatrixBuilder.build_transition_matrix(g)
    p = compute_stationary_distribution(M, alpha=0.85)
    scale = compute_component_scale(10)
    scores = compute_page_authority_scores(M, p, comp_scale=scale)

    for k, score in scores.items():
        assert 0.0 <= score <= 1.0


# ==============================================================================
# 7. REAL DATABASE READ-ONLY INTEGRATION TEST (ADR-04)
# ==============================================================================

def test_database_extraction_read_only_mode():
    """Verify real extraction against linkpilot.db in strictly read-only mode."""
    db_path = "./data/linkpilot.db"
    assert os.path.exists(db_path)

    graph = build_observed_graph_from_db(db_path=db_path, require_read_only=True)
    assert len(graph.vertices) >= 150
    assert graph.total_edge_count >= 100

    # Ensure self-loops are detected
    assert len(graph.self_loops) >= 1

    # WCC decomposition
    wccs = graph.find_weakly_connected_components()
    assert len(wccs) >= 1
    largest_wcc = wccs[0]
    assert largest_wcc.node_count >= 50

    # Build matrix and compute PageRank on largest component
    M = CSRMatrixBuilder.build_transition_matrix(graph, component=largest_wcc)
    assert M.nrows == largest_wcc.node_count
    assert M.ncols == largest_wcc.node_count

    p = compute_stationary_distribution(M, alpha=0.85)
    assert len(p) == largest_wcc.node_count
    assert abs(sum(p) - 1.0) < 1e-6


# ==============================================================================
# 8. ADDITIONAL EDGE CASES & MEMORY BENCHMARKS
# ==============================================================================

def test_dangling_nodes_identification():
    """Verify dangling nodes (out-degree 0) are identified accurately."""
    g = PhysicalGraph()
    for i in (0, 1, 2):
        k = f"https://example.com/p{i}"
        g.add_vertex(GraphVertex(
            key=k, raw_url=k, normalized_url=k, domain="example.com",
            site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
            is_authoritative_scored_page=True
        ))
    # Only 0 -> 1. Nodes 1 and 2 are dangling.
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key="https://example.com/p0", target_key="https://example.com/p1", raw_href=""))
    g.finalize()

    M = CSRMatrixBuilder.build_transition_matrix(g)
    dangling = identify_dangling_nodes(M)

    idx0 = M.key_to_idx["https://example.com/p0"]
    idx1 = M.key_to_idx["https://example.com/p1"]
    idx2 = M.key_to_idx["https://example.com/p2"]

    assert idx0 not in dangling
    assert idx1 in dangling
    assert idx2 in dangling


def test_single_vertex_graph():
    """Verify single-node graph computes stationary distribution and score without division by zero."""
    g = PhysicalGraph()
    k = "https://example.com/single"
    g.add_vertex(GraphVertex(
        key=k, raw_url=k, normalized_url=k, domain="example.com",
        site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
        is_authoritative_scored_page=True
    ))
    g.finalize()

    wccs = g.find_weakly_connected_components()
    assert len(wccs) == 1
    M = CSRMatrixBuilder.build_transition_matrix(g, component=wccs[0])
    assert M.shape == (1, 1)
    p = compute_stationary_distribution(M)
    assert p == [1.0]

    scores = compute_page_authority_scores(M, p, comp_scale=wccs[0].comp_scale)
    assert k in scores
    assert 0.0 <= scores[k] <= 1.0


def test_all_dangling_vertices_graph():
    """Verify graph with no edges distributes probability uniformly across all nodes."""
    g = PhysicalGraph()
    for i in range(4):
        k = f"https://example.com/iso{i}"
        g.add_vertex(GraphVertex(
            key=k, raw_url=k, normalized_url=k, domain="example.com",
            site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
            is_authoritative_scored_page=True
        ))
    g.finalize()

    M = CSRMatrixBuilder.build_transition_matrix(g)
    p = compute_stationary_distribution(M, alpha=0.85)

    assert len(p) == 4
    for val in p:
        assert abs(val - 0.25) < 1e-6


def test_get_subgraph_wcc_isolation():
    """Verify get_subgraph extracts strictly the vertices and edges belonging to the WCC."""
    g = PhysicalGraph()
    for i in range(4):
        k = f"https://example.com/{i}"
        g.add_vertex(GraphVertex(
            key=k, raw_url=k, normalized_url=k, domain="example.com",
            site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
            is_authoritative_scored_page=True
        ))
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key="https://example.com/0", target_key="https://example.com/1", raw_href=""))
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key="https://example.com/2", target_key="https://example.com/3", raw_href=""))
    g.finalize()

    wccs = g.find_weakly_connected_components()
    assert len(wccs) == 2

    sub0 = g.get_subgraph(wccs[0])
    assert len(sub0.vertices) == 2
    assert sub0.total_edge_count == 1


def test_in_degree_and_out_degree_calculation():
    """Verify in-degree and out-degree calculations with and without self-loops."""
    g = PhysicalGraph()
    k1 = "https://example.com/a"
    k2 = "https://example.com/b"
    for k in (k1, k2):
        g.add_vertex(GraphVertex(
            key=k, raw_url=k, normalized_url=k, domain="example.com",
            site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
            is_authoritative_scored_page=True
        ))

    # a -> a (self-loop, weight 1.0)
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key=k1, target_key=k1, raw_href="", dom_context="body"))
    # a -> b (weight 1.0)
    g.add_edge_occurrence(PhysicalEdgeOccurrence(source_key=k1, target_key=k2, raw_href="", dom_context="body"))
    g.finalize()

    # Excluding self-loops
    assert abs(g.get_out_degree(k1, exclude_self_loops=True) - 1.0) < 1e-6
    # Including self-loops
    assert abs(g.get_out_degree(k1, exclude_self_loops=False) - 2.0) < 1e-6

    # In-degrees
    assert abs(g.get_in_degree(k2, exclude_self_loops=True) - 1.0) < 1e-6
    assert abs(g.get_in_degree(k1, exclude_self_loops=True) - 0.0) < 1e-6
    assert abs(g.get_in_degree(k1, exclude_self_loops=False) - 1.0) < 1e-6


def test_rel_compound_ugc_and_follow():
    """Verify compound rel attributes like 'ugc, noopener'."""
    assert compute_rel_multiplier("ugc, noopener") == 0.5
    assert compute_rel_multiplier("external nofollow") == 0.0


def test_large_sparse_synthetic_graph_memory_benchmark():
    """Benchmark CSR memory on a 1,000-node, 5,000-edge synthetic graph."""
    g = PhysicalGraph()
    n = 1000
    for i in range(n):
        k = f"https://example.com/node{i}"
        g.add_vertex(GraphVertex(
            key=k, raw_url=k, normalized_url=k, domain="example.com",
            site_boundary="example.com", vertex_type=VertexType.INTERNAL_CRAWLED,
            is_authoritative_scored_page=True
        ))

    # Add 5,000 forward edges
    for i in range(n):
        for j in range(1, 6):
            target_i = (i + j) % n
            g.add_edge_occurrence(PhysicalEdgeOccurrence(
                source_key=f"https://example.com/node{i}",
                target_key=f"https://example.com/node{target_i}",
                raw_href=""
            ))
    g.finalize()

    M = CSRMatrixBuilder.build_transition_matrix(g)
    assert M.shape == (1000, 1000)
    assert M.nnz == 5000

    # Theoretical: 12 * 5000 + 4 * 1000 + 4 = 64004 bytes ≈ 62.5 KB
    actual_mem = M.memory_bytes()
    expected_mem = 12 * 5000 + 4 * 1000 + 4
    assert actual_mem == expected_mem
    assert actual_mem < 70000  # Well under 70 KB!

    # Solve PageRank
    p = compute_stationary_distribution(M, alpha=0.85, max_iter=50)
    assert len(p) == 1000
    assert abs(sum(p) - 1.0) < 1e-6


def test_read_only_sqlite_invariance():
    """Verify that querying linkpilot.db does not mutate the database file."""
    db_path = "./data/linkpilot.db"
    stat_before = os.stat(db_path)

    graph = build_observed_graph_from_db(db_path=db_path, require_read_only=True)
    assert len(graph.vertices) > 0

    stat_after = os.stat(db_path)
    assert stat_before.st_size == stat_after.st_size
    assert stat_before.st_mtime == stat_after.st_mtime
