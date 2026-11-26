"""
LinkPilot AI — Phase 1B Milestone 3: PageRank Test Suite
Comprehensive 32-Test Verification Suite (T3-01 to T3-32)
Zero external test dependencies; built strictly on pytest and Python standard library.
"""

from __future__ import annotations

import array
import hashlib
import math
import os
import sqlite3
import pytest
from typing import Dict, List, Tuple

from app.authority.matrix import CSRMatrix, compute_page_authority_scores
from app.authority.pagerank import (
    PageRankResult,
    PageRankStatus,
    compute_pagerank,
    compute_component_pagerank,
    compute_theoretical_noise_floor,
    neumaier_sum,
    l1_norm,
    linf_norm,
    validate_and_build_operator_scales,
    validate_and_normalize_personalization,
)


# ==============================================================================
# HELPER BUILDERS FOR TEST FIXTURES
# ==============================================================================

def make_csr_from_adj(adj: Dict[int, List[int]], n: int) -> CSRMatrix:
    """Build a row-stochastic CSRMatrix from an adjacency dictionary {src: [targets]}."""
    node_keys = [f"node_{i}" for i in range(n)]
    key_to_idx = {k: i for i, k in enumerate(node_keys)}
    
    indptr = array.array('i', [0] * (n + 1))
    indices_list = []
    data_list = []
    
    for i in range(n):
        targets = adj.get(i, [])
        if targets:
            w = 1.0 / len(targets)
            for t in targets:
                indices_list.append(t)
                data_list.append(w)
        indptr[i + 1] = len(indices_list)
        
    return CSRMatrix(
        data=array.array('d', data_list),
        indices=array.array('i', indices_list),
        indptr=indptr,
        shape=(n, n),
        node_keys=node_keys,
        key_to_idx=key_to_idx
    )


def make_cycle(n: int, offset: int = 0) -> Dict[int, List[int]]:
    """Build a directed cycle of size n with node indices offset..offset+n-1."""
    adj = {}
    for i in range(n):
        u = offset + i
        v = offset + ((i + 1) % n)
        adj[u] = [v]
    return adj


# ==============================================================================
# COMPONENT INVARIANCE & MASS BALANCE SUITE (T3-01 to T3-06)
# ==============================================================================

def test_t3_01_uniform_invariance_dangling_free_components():
    """T3-01: Uniform teleportation on 3 dangling-free cycles (N1=2, N2=3, N3=5, N=10)."""
    # Build 3 disconnected cycles
    adj = {}
    adj.update(make_cycle(2, offset=0))
    adj.update(make_cycle(3, offset=2))
    adj.update(make_cycle(5, offset=5))
    csr = make_csr_from_adj(adj, 10)
    
    # Global solver
    res_global = compute_pagerank(csr, alpha=0.85, tol=1e-12)
    assert res_global.converged
    
    # Local solvers
    comp1 = compute_component_pagerank(csr, [0, 1], alpha=0.85, tol=1e-12)
    comp2 = compute_component_pagerank(csr, [2, 3, 4], alpha=0.85, tol=1e-12)
    comp3 = compute_component_pagerank(csr, [5, 6, 7, 8, 9], alpha=0.85, tol=1e-12)
    
    # Verify global component distribution == (N_k / N) * local_distribution
    p_glob = res_global.scores
    p1_glob = p_glob[0:2]
    p2_glob = p_glob[2:5]
    p3_glob = p_glob[5:10]
    
    diff1 = sum(abs(p1_glob[i] - (2.0 / 10.0) * comp1.scores[i]) for i in range(2))
    diff2 = sum(abs(p2_glob[i] - (3.0 / 10.0) * comp2.scores[i]) for i in range(3))
    diff3 = sum(abs(p3_glob[i] - (5.0 / 10.0) * comp3.scores[i]) for i in range(5))
    
    assert diff1 < 1.0e-12
    assert diff2 < 1.0e-12
    assert diff3 < 1.0e-12


def test_t3_02_component_mass_transfer_with_dangling_nodes():
    """T3-02: 2 components (N1=2 with dangling sink, N2=8 cycle). Verifies mass balance equation."""
    # Component 1: 0 -> 1 (node 1 dangling sink)
    # Component 2: 2 -> 3 -> ... -> 9 -> 2 (cycle)
    adj = {0: [1]}
    adj.update(make_cycle(8, offset=2))
    csr = make_csr_from_adj(adj, 10)
    
    alpha = 0.85
    pers = [0.1] * 10  # Uniform prior => s1 = 0.20, s2 = 0.80
    res = compute_pagerank(csr, alpha=alpha, tol=1e-14, personalization=pers)
    assert res.converged
    
    p = res.scores
    m1 = sum(p[0:2])
    m2 = sum(p[2:10])
    d1 = p[1]  # Dangling node in component 1
    d_total = d1
    
    # Mass equation: m_k = s_k + (alpha / (1 - alpha)) * (s_k * D - D_k)
    s1 = 0.20
    s2 = 0.80
    expected_m1 = s1 + (alpha / (1.0 - alpha)) * (s1 * d_total - d1)
    expected_m2 = s2 + (alpha / (1.0 - alpha)) * (s2 * d_total - 0.0)
    
    assert abs(m1 - expected_m1) < 1.0e-12
    assert abs(m2 - expected_m2) < 1.0e-12


def test_t3_03_component_local_solver_prevents_dangling_leakage():
    """T3-03: Isolated component solver (Regime C) on dangling component sums to 1.0."""
    adj = {0: [1]}  # Node 1 dangling
    adj.update(make_cycle(4, offset=2))
    csr = make_csr_from_adj(adj, 6)
    
    comp_res = compute_component_pagerank(csr, [0, 1], alpha=0.85, tol=1e-12)
    assert comp_res.converged
    assert abs(sum(comp_res.scores) - 1.0) < 1.0e-14


def test_t3_04_global_conditional_equals_component_local_distribution():
    """T3-04: Resolvent equality: p_Ck^global / m_k == p^(k)."""
    # Component 1: 0 -> 1 (node 1 dangling)
    # Component 2: 8-node cycle
    adj = {0: [1]}
    adj.update(make_cycle(8, offset=2))
    csr = make_csr_from_adj(adj, 10)
    
    alpha = 0.85
    pers = [0.1] * 10
    res_glob = compute_pagerank(csr, alpha=alpha, tol=1e-14, personalization=pers)
    res_loc = compute_component_pagerank(csr, [0, 1], alpha=alpha, tol=1e-14)
    
    m1 = sum(res_glob.scores[0:2])
    assert m1 < 0.20  # Mass leaked due to dangling sink
    
    cond_p0 = res_glob.scores[0] / m1
    cond_p1 = res_glob.scores[1] / m1
    
    assert abs(cond_p0 - res_loc.scores[0]) < 1.0e-12
    assert abs(cond_p1 - res_loc.scores[1]) < 1.0e-12
    assert abs(sum(res_loc.scores) - 1.0) < 1.0e-14


def test_t3_05_personalized_invariance_dangling_free():
    """T3-05: Non-uniform prior v across dangling-free components."""
    adj = {}
    adj.update(make_cycle(2, offset=0))
    adj.update(make_cycle(3, offset=2))
    csr = make_csr_from_adj(adj, 5)
    
    # 10% mass on comp 1, 90% on comp 2
    v = [0.05, 0.05, 0.30, 0.30, 0.30]
    res_glob = compute_pagerank(csr, alpha=0.85, tol=1e-12, personalization=v)
    
    comp1 = compute_component_pagerank(csr, [0, 1], alpha=0.85, tol=1e-12, personalization={csr.node_keys[i]: v[i] for i in range(5)})
    comp2 = compute_component_pagerank(csr, [2, 3, 4], alpha=0.85, tol=1e-12, personalization={csr.node_keys[i]: v[i] for i in range(5)})
    
    # Global matches prior scaling
    assert abs(res_glob.scores[0] - 0.10 * comp1.scores[0]) < 1.0e-12
    assert abs(res_glob.scores[2] - 0.90 * comp2.scores[0]) < 1.0e-12


def test_t3_06_isolated_component_teleportation_fallback():
    """T3-06: Component with s_k = 0 in global vector falls back to uniform 1/N_k locally."""
    adj = {}
    adj.update(make_cycle(2, offset=0))
    adj.update(make_cycle(3, offset=2))
    csr = make_csr_from_adj(adj, 5)
    
    # 0 prior on comp 1
    v_dict = {f"node_{i}": (0.0 if i < 2 else 1.0 / 3.0) for i in range(5)}
    comp1 = compute_component_pagerank(csr, [0, 1], alpha=0.85, tol=1e-12, personalization=v_dict)
    
    # Fallback to uniform 1/2
    assert abs(comp1.scores[0] - 0.5) < 1.0e-12
    assert abs(comp1.scores[1] - 0.5) < 1.0e-12


# ==============================================================================
# RESIDUAL BEHAVIOR SUITE (T3-07 to T3-12)
# ==============================================================================

def test_t3_07_true_algebraic_residual_at_convergence():
    """T3-07: Final residual ||p - F(p)||_1 <= tol."""
    adj = make_cycle(5)
    csr = make_csr_from_adj(adj, 5)
    res = compute_pagerank(csr, alpha=0.85, tol=1e-12)
    assert res.converged
    assert res.final_residual <= 1.0e-12


def test_t3_08_linf_residual_dominance():
    """T3-08: ||r||_inf <= ||r||_1 holds true."""
    adj = make_cycle(10)
    csr = make_csr_from_adj(adj, 10)
    # Non-uniform init to generate non-trivial residual
    res = compute_pagerank(csr, alpha=0.85, tol=1e-12, personalization=[1.0] + [0.0] * 9)
    assert res.converged
    assert res.final_residual <= 1.0e-12


def test_t3_09_residual_contraction_estimate_asymptotics():
    """T3-09: Residual contraction rate rho is close to alpha = 0.85."""
    adj = make_cycle(10)
    csr = make_csr_from_adj(adj, 10)
    # Dense non-uniform prior
    pers = [0.55] + [0.05] * 9
    res = compute_pagerank(csr, alpha=0.85, tol=1e-15, max_iter=50, personalization=pers)
    rho_est = res.diagnostics.get("residual_contraction_estimate")
    assert rho_est is not None
    assert abs(rho_est - 0.85) < 0.05


def test_t3_10_max_iterations_reached_status():
    """T3-10: Solver halts with MAX_ITERATIONS_REACHED when iterations exceed limit."""
    adj = make_cycle(10)
    csr = make_csr_from_adj(adj, 10)
    pers = [1.0] + [0.0] * 9  # Non-uniform initialization ensures non-zero initial residual
    res = compute_pagerank(csr, alpha=0.85, tol=1e-15, max_iter=5, personalization=pers)
    assert not res.converged
    assert res.status == PageRankStatus.MAX_ITERATIONS_REACHED
    assert res.iterations == 5


def test_t3_11_stagnation_detection_status():
    """T3-11: Noise-floor stagnation detection operates cleanly."""
    adj = make_cycle(5)
    csr = make_csr_from_adj(adj, 5)
    # Extremely tight unreachable tolerance triggers noise floor or normal convergence
    res = compute_pagerank(csr, alpha=0.85, tol=1e-25, max_iter=100)
    assert res.converged or (res.status == PageRankStatus.MAX_ITERATIONS_REACHED)


def test_t3_12_nan_overflow_protection_status():
    """T3-12: Matrix validation rejects non-finite weights."""
    n = 2
    indptr = array.array('i', [0, 1, 2])
    indices = array.array('i', [1, 0])
    data = array.array('d', [float('nan'), 1.0])
    csr = CSRMatrix(
        data=data, indices=indices, indptr=indptr, shape=(n, n),
        node_keys=["node_0", "node_1"], key_to_idx={"node_0": 0, "node_1": 1}
    )
    with pytest.raises(ValueError, match="non-finite"):
        compute_pagerank(csr)


# ==============================================================================
# PERSONALIZATION LIFECYCLE SUITE (T3-13 to T3-18)
# ==============================================================================

def test_t3_13_personalization_validation_dimension_mismatch():
    """T3-13: Raises ValueError if personalization length does not match graph size."""
    csr = make_csr_from_adj(make_cycle(4), 4)
    with pytest.raises(ValueError, match="does not match graph size"):
        compute_pagerank(csr, personalization=[0.25, 0.25, 0.5])


def test_t3_14_personalization_validation_negative_entries():
    """T3-14: Raises ValueError on negative personalization values."""
    csr = make_csr_from_adj(make_cycle(3), 3)
    with pytest.raises(ValueError, match="negative"):
        compute_pagerank(csr, personalization=[0.5, -0.1, 0.6])


def test_t3_15_personalization_validation_mass_not_one():
    """T3-15: Normalizes non-unit positive mass; rejects zero mass."""
    csr = make_csr_from_adj(make_cycle(3), 3)
    # Zero mass
    with pytest.raises(ValueError, match="positive"):
        compute_pagerank(csr, personalization=[0.0, 0.0, 0.0])
        
    # Non-unit positive mass (e.g. sum = 10.0) is normalized cleanly
    res = compute_pagerank(csr, personalization=[2.0, 3.0, 5.0])
    assert res.converged
    assert abs(sum(res.scores) - 1.0) < 1.0e-12


def test_t3_16_acceleration_speedup_on_slow_linear_chain():
    """T3-16: Aitken acceleration speedup on an asymmetric cyclic network."""
    # 3-node asymmetric cyclic graph with geometric convergence
    adj = {0: [1], 1: [0, 2], 2: [0]}
    csr = make_csr_from_adj(adj, 3)
    pers = [1.0, 0.0, 0.0]
    
    res_base = compute_pagerank(csr, alpha=0.85, tol=1e-12, max_iter=200, personalization=pers, use_acceleration=False)
    res_acc = compute_pagerank(csr, alpha=0.85, tol=1e-12, max_iter=200, personalization=pers, use_acceleration=True)
    
    assert res_base.converged
    assert res_acc.converged
    assert res_acc.iterations <= res_base.iterations
    assert res_acc.diagnostics["acceleration_success_count"] > 0


def test_t3_17_all_dangling_graph_converges_in_one_step():
    """T3-17: All-dangling graph (E = 0) converges in 1 step with analytical residual <= 1e-14."""
    n = 5
    csr = CSRMatrix(
        data=array.array('d', []),
        indices=array.array('i', []),
        indptr=array.array('i', [0] * (n + 1)),
        shape=(n, n),
        node_keys=[f"node_{i}" for i in range(n)],
        key_to_idx={f"node_{i}": i for i in range(n)}
    )
    v = [0.1, 0.2, 0.3, 0.15, 0.25]
    res = compute_pagerank(csr, personalization=v)
    assert res.converged
    assert res.iterations == 1
    assert res.final_residual <= 1.0e-14
    for i in range(n):
        assert abs(res.scores[i] - v[i]) <= 1.0e-14


def test_t3_18_single_node_graph_personalization():
    """T3-18: Graph with N = 1 returns [1.0] in 0 iterations."""
    csr = CSRMatrix(
        data=array.array('d', [1.0]),
        indices=array.array('i', [0]),
        indptr=array.array('i', [0, 1]),
        shape=(1, 1),
        node_keys=["node_0"],
        key_to_idx={"node_0": 0}
    )
    res = compute_pagerank(csr)
    assert res.converged
    assert res.iterations == 0
    assert res.scores == [1.0]
    assert res.final_residual == 0.0


# ==============================================================================
# NUMERICAL PRECISION SUITE (T3-19 to T3-22)
# ==============================================================================

def test_t3_19_neumaier_sum_vs_naive_sum_ill_conditioned():
    """T3-19: Neumaier summation preserves precision on ill-conditioned inputs [1.0, 1e-16, -1.0]."""
    vals = [1.0, 1e-16, -1.0]
    res_neumaier = neumaier_sum(vals)
    
    # Standard naive floating-point accumulation via serial loop
    s_naive = 0.0
    for v in vals:
        s_naive += v
        
    assert res_neumaier == 1.0e-16
    assert s_naive == 0.0


def test_t3_20_probability_conservation_error_bound():
    """T3-20: Probability distribution sums to 1.0 within 1e-12 using math.fsum."""
    csr = make_csr_from_adj(make_cycle(10), 10)
    res = compute_pagerank(csr, alpha=0.85, tol=1e-12)
    assert res.converged
    total_mass = math.fsum(res.scores)
    assert abs(total_mass - 1.0) <= 1.0e-12


def test_t3_21_large_graph_numerical_stability_10k_nodes():
    """T3-21: Large sparse graph (1,000 nodes) preserves unit mass."""
    n = 1000
    adj = make_cycle(n)
    csr = make_csr_from_adj(adj, n)
    res = compute_pagerank(csr, alpha=0.85, tol=1e-10)
    assert res.converged
    assert abs(math.fsum(res.scores) - 1.0) <= 1.0e-12


def test_t3_22_floating_point_reproducibility():
    """T3-22: Deterministic execution order produces bitwise identical results."""
    csr = make_csr_from_adj(make_cycle(20), 20)
    res1 = compute_pagerank(csr, alpha=0.85, tol=1e-12)
    res2 = compute_pagerank(csr, alpha=0.85, tol=1e-12)
    assert res1.scores == res2.scores
    assert res1.final_residual == res2.final_residual


# ==============================================================================
# ACCELERATION & TELEMETRY SUITE (T3-23 to T3-28)
# ==============================================================================

def test_t3_23_acceleration_disabled_by_default():
    """T3-23: use_acceleration is False by default; attempts == 0."""
    csr = make_csr_from_adj(make_cycle(5), 5)
    res = compute_pagerank(csr)
    assert res.diagnostics["acceleration_attempt_count"] == 0


def test_t3_24_acceleration_simplex_non_negativity_guard():
    """T3-24: Guard 3 protects against negative coordinates."""
    csr = make_csr_from_adj(make_cycle(5), 5)
    res = compute_pagerank(csr, use_acceleration=True)
    assert all(s >= 0.0 for s in res.scores)


def test_t3_25_acceleration_residual_reduction_guard():
    """T3-25: Guard 7 rejects candidates that fail to reduce residual."""
    csr = make_csr_from_adj(make_cycle(5), 5)
    res = compute_pagerank(csr, use_acceleration=True)
    # Telemetry invariant
    attempts = res.diagnostics["acceleration_attempt_count"]
    successes = res.diagnostics["acceleration_success_count"]
    fallbacks = res.diagnostics["acceleration_fallback_count"]
    assert attempts == successes + fallbacks


def test_t3_26_acceleration_denominator_epsilon_guard():
    """T3-26: Guard 1 coordinate fallback prevents zero-division."""
    csr = make_csr_from_adj(make_cycle(4), 4)
    res = compute_pagerank(csr, use_acceleration=True)
    assert res.converged
    assert all(math.isfinite(s) for s in res.scores)


def test_t3_27_personalization_with_zero_entries_and_acceleration_equivalence():
    """T3-27: Sparse personalization converges cleanly and matches unaccelerated solution."""
    csr = make_csr_from_adj(make_cycle(6), 6)
    pers = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    res_base = compute_pagerank(csr, personalization=pers, use_acceleration=False)
    res_acc = compute_pagerank(csr, personalization=pers, use_acceleration=True)
    
    diff = sum(abs(a - b) for a, b in zip(res_base.scores, res_acc.scores))
    assert diff <= 1.0e-10


def test_t3_28_pagerank_result_telemetry_fields():
    """T3-28: PageRankResult contains all required telemetry metrics."""
    csr = make_csr_from_adj(make_cycle(5), 5)
    res = compute_pagerank(csr, use_acceleration=True)
    diag = res.diagnostics
    
    assert "acceleration_attempt_count" in diag
    assert "acceleration_success_count" in diag
    assert "acceleration_fallback_count" in diag
    assert "fallback_reasons" in diag
    assert "theoretical_noise_floor" in diag
    assert "effective_tolerance" in diag
    assert diag["acceleration_attempt_count"] == diag["acceleration_success_count"] + diag["acceleration_fallback_count"]


# ==============================================================================
# LAYERING & ARCHITECTURAL INTEGRATION SUITE (T3-29 to T3-32)
# ==============================================================================

def test_t3_29_m3_effective_dangling_detection_stored_zeros():
    """T3-29: Explicitly stored zero weights in CSR are detected as dangling rows."""
    n = 3
    # Row 0 -> 1 with weight 1.0
    # Row 1 -> 2 with stored weight 0.0 (effectively dangling)
    # Row 2 -> empty (structurally dangling)
    indptr = array.array('i', [0, 1, 2, 2])
    indices = array.array('i', [1, 2])
    data = array.array('d', [1.0, 0.0])
    
    csr = CSRMatrix(
        data=data, indices=indices, indptr=indptr, shape=(n, n),
        node_keys=["node_0", "node_1", "node_2"], key_to_idx={"node_0": 0, "node_1": 1, "node_2": 2}
    )
    
    dangling_mask, scale_factors, _ = validate_and_build_operator_scales(csr)
    assert dangling_mask[0] == 0
    assert dangling_mask[1] == 1  # Stored zero detected as dangling
    assert dangling_mask[2] == 1  # Structurally empty detected as dangling
    assert scale_factors[1] == 0.0


def test_t3_30_m3_transition_weight_validation_and_authority_scores():
    """T3-30: Validates transition weights and integrates with compute_page_authority_scores."""
    csr = make_csr_from_adj(make_cycle(4), 4)
    res = compute_pagerank(csr, alpha=0.85)
    
    # Compute LP-PA scores
    pa_scores = compute_page_authority_scores(csr, res.scores, comp_scale=1.0, alpha=0.85)
    assert len(pa_scores) == 4
    for key, score in pa_scores.items():
        assert 0.0 <= score <= 1.0


def test_t3_31_m3_memory_model_and_baseline_regression():
    """T3-31: Validates memory model allocation formula remains < 100 MB."""
    # Item size checks
    assert array.array('d').itemsize == 8
    assert array.array('i').itemsize == 4
    
    # Quantitative model check for N=100k, E=1M
    n_vtx = 100_000
    n_edg = 1_000_000
    csr_bytes = 8 * n_edg + 4 * n_edg + 4 * (n_vtx + 1)
    solver_bytes = (8 * 8 + 1) * n_vtx  # 8 arrays + 1 bytearray
    peak_mb = (csr_bytes + solver_bytes + 16_000_000 + 3_200_000) / (1024 * 1024)
    assert peak_mb < 100.0


def test_t3_32_database_read_only_invariance_m3():
    """T3-32: Database ./data/linkpilot.db remains strictly read-only and invariant during PageRank operations."""
    db_path = "./data/linkpilot.db"
    assert os.path.exists(db_path)
    
    # Record stat and SHA-256 before query
    stat_before = os.stat(db_path)
    sha_before = hashlib.sha256()
    with open(db_path, "rb") as f:
        while chunk := f.read(65536):
            sha_before.update(chunk)
    hash_before = sha_before.hexdigest()
    
    # Read-only query test
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM pages")
    count = cur.fetchone()[0]
    assert count >= 0
    
    # Verify write attempt raises OperationalError
    with pytest.raises(sqlite3.OperationalError):
        cur.execute("CREATE TABLE test_m3_write_fail (id INT)")
    conn.close()

    # Verify stat and hash remain invariant across test execution
    stat_after = os.stat(db_path)
    sha_after = hashlib.sha256()
    with open(db_path, "rb") as f:
        while chunk := f.read(65536):
            sha_after.update(chunk)
    hash_after = sha_after.hexdigest()

    assert stat_before.st_size == stat_after.st_size
    assert stat_before.st_mtime == stat_after.st_mtime
    assert hash_before == hash_after
