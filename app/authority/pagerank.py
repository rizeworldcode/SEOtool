"""
LinkPilot AI — Phase 1B Milestone 3: PageRank Numerical Reliability & Convergence Hardening.
High-performance, mathematically rigorous PageRank engine built strictly on Python standard library.
Zero external package dependencies.
"""

from __future__ import annotations

import array
from dataclasses import dataclass, field
from enum import Enum
import math
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from app.authority.matrix import CSRMatrix


# ==============================================================================
# ENUMS & DATA CONTRACTS
# ==============================================================================

class PageRankStatus(str, Enum):
    """Terminal convergence or stopping status of the PageRank solver."""
    CONVERGED = "CONVERGED"
    MAX_ITERATIONS_REACHED = "MAX_ITERATIONS_REACHED"
    STAGNATED = "STAGNATED"
    STAGNATED_AT_NOISE_FLOOR = "STAGNATED_AT_NOISE_FLOOR"
    NUMERICAL_OVERFLOW_OR_NAN = "NUMERICAL_OVERFLOW_OR_NAN"


@dataclass
class PageRankResult:
    """
    Standard output contract for Milestone 3 PageRank computation.
    Contains final score distribution, convergence metadata, and telemetry.
    """
    scores: List[float]
    converged: bool
    iterations: int
    final_residual: float
    status: PageRankStatus
    diagnostics: Dict[str, Any] = field(default_factory=dict)


# ==============================================================================
# NUMERICAL PRIMITIVES & NEUMAIER COMPENSATED SUMMATION
# ==============================================================================

UNIT_ROUNDOFF: float = 2.0 ** -53  # ~1.1102230246251565e-16 (binary64)
MACHINE_EPSILON: float = 2.0 ** -52  # ~2.220446049250313e-16


def neumaier_sum(values: Union[Sequence[float], array.array]) -> float:
    """
    Kahan-Neumaier compensated floating-point summation algorithm (Neumaier, 1974).
    Accurately tracks rounding errors regardless of whether the summand or running
    accumulator has larger magnitude.
    
    For non-negative terms summing to S, worst-case error is bounded by (2u + O(Nu^2)) * S.
    """
    s = 0.0
    c = 0.0
    for val in values:
        t = s + val
        if abs(s) >= abs(val):
            c += (s - t) + val
        else:
            c += (val - t) + s
        s = t
    return s + c


def l1_norm(vector: Union[Sequence[float], array.array]) -> float:
    """Compute exact L1 norm sum(|x_i|) using Neumaier compensated summation."""
    s = 0.0
    c = 0.0
    for val in vector:
        mag = abs(val)
        t = s + mag
        if abs(s) >= abs(mag):
            c += (s - t) + mag
        else:
            c += (mag - t) + s
        s = t
    return s + c


def linf_norm(vector: Union[Sequence[float], array.array]) -> float:
    """Compute Linf norm max(|x_i|)."""
    max_val = 0.0
    for val in vector:
        mag = abs(val)
        if mag > max_val:
            max_val = mag
    return max_val


# ==============================================================================
# MATRIX VALIDATION & ROW-SCALE VECTOR CONSTRUCTION
# ==============================================================================

def validate_and_build_operator_scales(
    matrix: CSRMatrix
) -> Tuple[bytearray, array.array, int]:
    """
    Validates CSR transition matrix and precomputes row scale factors and in-degrees.
    
    Invariants Enforced:
    1. Every non-zero entry must be finite and non-negative (>= 0.0).
    2. Effective row sum S_i is accumulated via Neumaier summation.
    3. If structurally empty (indptr[i+1] == indptr[i]) or S_i == 0.0:
       Row is DANGLING (mask[i] = 1, sigma[i] = 0.0).
    4. If S_i > 0.0:
       Row must satisfy |S_i - 1.0| <= 1e-7 (row-stochastic contract).
       If valid: mask[i] = 0, sigma[i] = 1.0 / S_i.
       If invalid: raises ValueError.
       
    Returns:
      (dangling_mask, scale_factors, max_in_degree)
    """
    n = matrix.nrows
    dangling_mask = bytearray(n)
    scale_factors = array.array('d', [0.0] * n)
    in_degrees = [0] * matrix.ncols

    for i in range(n):
        start = matrix.indptr[i]
        end = matrix.indptr[i + 1]

        # Case A: Structurally empty row
        if start == end:
            dangling_mask[i] = 1
            scale_factors[i] = 0.0
            continue

        # Case B: Stored entries
        row_vals = []
        for k in range(start, end):
            val = matrix.data[k]
            if not math.isfinite(val):
                raise ValueError(f"Transition weight at row {i}, index {k} is non-finite: {val}")
            if val < 0.0:
                raise ValueError(f"Transition weight at row {i}, index {k} is negative: {val}")
            col = matrix.indices[k]
            in_degrees[col] += 1
            row_vals.append(val)

        row_sum = neumaier_sum(row_vals)

        if row_sum == 0.0:
            # Explicitly stored zeros: row is effectively dangling
            dangling_mask[i] = 1
            scale_factors[i] = 0.0
        elif abs(row_sum - 1.0) <= 1e-7:
            dangling_mask[i] = 0
            scale_factors[i] = 1.0 / row_sum
        else:
            raise ValueError(
                f"Row {i} is unnormalized: effective sum {row_sum} deviates from 1.0 by {abs(row_sum - 1.0)}"
            )

    max_in_degree = max(in_degrees) if in_degrees else 0
    return dangling_mask, scale_factors, max_in_degree


def validate_and_normalize_personalization(
    personalization: Optional[Union[Dict[str, float], Sequence[float]]],
    n: int,
    node_keys: Optional[List[str]] = None
) -> array.array:
    """
    Validates and normalizes personalization vector v onto the unit probability simplex.
    
    Invariants Enforced:
    1. Dimension match: len(v) == n.
    2. Finite non-negativity: v_i >= 0.0 and math.isfinite(v_i).
    3. Positive mass: sum(v_i) > 0.0.
    4. Simplex normalization: projected to sum(v_i) == 1.0 via Neumaier sum.
    """
    if n == 0:
        return array.array('d', [])

    v = array.array('d', [0.0] * n)

    if personalization is None:
        # Uniform prior: 1/N
        inv_n = 1.0 / float(n)
        for i in range(n):
            v[i] = inv_n
        return v

    if isinstance(personalization, dict):
        if node_keys is None:
            raise ValueError("node_keys required to map personalization dictionary")
        for i, key in enumerate(node_keys):
            val = float(personalization.get(key, 0.0))
            if not math.isfinite(val):
                raise ValueError(f"Personalization value for key '{key}' is non-finite: {val}")
            if val < 0.0:
                raise ValueError(f"Personalization value for key '{key}' is negative: {val}")
            v[i] = val
    else:
        if len(personalization) != n:
            raise ValueError(f"Personalization length {len(personalization)} does not match graph size {n}")
        for i in range(n):
            val = float(personalization[i])
            if not math.isfinite(val):
                raise ValueError(f"Personalization entry {i} is non-finite: {val}")
            if val < 0.0:
                raise ValueError(f"Personalization entry {i} is negative: {val}")
            v[i] = val

    total_mass = neumaier_sum(v)
    if not math.isfinite(total_mass) or total_mass <= 0.0:
        raise ValueError(f"Personalization total mass must be positive and finite, got {total_mass}")

    # Normalize to unit simplex if deviation exceeds roundoff
    if abs(total_mass - 1.0) > 1e-12:
        for i in range(n):
            v[i] /= total_mass

    # Post-normalization check
    post_sum = neumaier_sum(v)
    if abs(post_sum - 1.0) > 2.0 * MACHINE_EPSILON * float(n):
        raise ValueError(f"Personalization normalization failed: post-sum {post_sum} != 1.0")

    return v


# ==============================================================================
# NUMERICAL RESOLUTION & NOISE FLOOR CALCULATIONS
# ==============================================================================

def compute_theoretical_noise_floor(max_in_degree: int, alpha: float) -> float:
    """
    Calculate the floating-point roundoff stagnation floor:
      tau_floor = (alpha * (d_in_max + 3) * u + 4 * u) / (1 - alpha)
    """
    denom = 1.0 - alpha
    if denom <= 0.0:
        denom = 0.01
    return (alpha * (max_in_degree + 3) * UNIT_ROUNDOFF + 4.0 * UNIT_ROUNDOFF) / denom


# ==============================================================================
# CORE SCALED OPERATOR & RESIDUAL EVALUATION
# ==============================================================================

def evaluate_pagerank_operator(
    matrix: CSRMatrix,
    scale_factors: array.array,
    dangling_mask: bytearray,
    p: array.array,
    v: array.array,
    alpha: float,
    workspace_y: array.array,
    scaled_p_workspace: array.array
) -> Tuple[array.array, float]:
    """
    Evaluates the mathematically stochastic PageRank operator:
      F(p) = alpha * M^T * (sigma (X) p) + (1 - alpha + alpha * D) * v
    
    Returns:
      (F(p) in workspace_y, dangling_mass D)
    """
    n = matrix.nrows

    # 1. Compute dangling mass D and scale non-dangling state probabilities
    dangling_mass_vals = []
    for i in range(n):
        p_val = p[i]
        if dangling_mask[i] == 1:
            dangling_mass_vals.append(p_val)
            scaled_p_workspace[i] = 0.0
        else:
            scaled_p_workspace[i] = p_val * scale_factors[i]

    d_mass = neumaier_sum(dangling_mass_vals)

    # 2. Transpose MVM: y_j = sum_{i in In(j)} M_ij * (sigma_i * p_i)
    # Zero out workspace_y
    for j in range(n):
        workspace_y[j] = 0.0

    for i in range(n):
        p_scaled = scaled_p_workspace[i]
        if p_scaled == 0.0:
            continue
        start = matrix.indptr[i]
        end = matrix.indptr[i + 1]
        for k in range(start, end):
            col = matrix.indices[k]
            workspace_y[col] += matrix.data[k] * p_scaled

    # 3. Apply damping factor alpha and teleportation injection
    mu = (1.0 - alpha) + alpha * d_mass
    for j in range(n):
        workspace_y[j] = alpha * workspace_y[j] + mu * v[j]

    return workspace_y, d_mass


def compute_production_residual(
    matrix: CSRMatrix,
    scale_factors: array.array,
    dangling_mask: bytearray,
    p: array.array,
    v: array.array,
    alpha: float,
    workspace_y: array.array,
    scaled_p_workspace: array.array,
    residual_workspace: array.array
) -> Tuple[float, float]:
    """
    Evaluates true algebraic residual vector r = p - F(p) and returns:
      (L1_norm(r), Linf_norm(r))
    """
    n = matrix.nrows
    f_p, d_mass = evaluate_pagerank_operator(
        matrix, scale_factors, dangling_mask, p, v, alpha, workspace_y, scaled_p_workspace
    )

    for i in range(n):
        residual_workspace[i] = p[i] - f_p[i]

    norm1 = l1_norm(residual_workspace)
    norm_inf = linf_norm(residual_workspace)
    return norm1, norm_inf


# ==============================================================================
# MAIN PAGERANK SOLVER ENGINE
# ==============================================================================

def compute_pagerank(
    matrix: CSRMatrix,
    alpha: float = 0.85,
    tol: float = 1e-12,
    max_iter: int = 200,
    personalization: Optional[Union[Dict[str, float], Sequence[float]]] = None,
    use_acceleration: bool = False,
    rtol: Optional[float] = None,
    atol: Optional[float] = None
) -> PageRankResult:
    """
    High-performance, mathematically rigorous PageRank solver with Aitken acceleration,
    effective dangling detection, row-stochastic scale vectors, and adaptive noise-floor policy.
    """
    n = matrix.nrows

    # Validate parameters
    if not (0.0 < alpha < 1.0):
        raise ValueError(f"Damping factor alpha must be in (0, 1), got {alpha}")
    if max_iter <= 0:
        raise ValueError(f"max_iter must be positive, got {max_iter}")
    if tol <= 0.0:
        raise ValueError(f"Tolerance must be positive, got {tol}")

    # Effective stopping tolerance
    target_atol = atol if atol is not None else tol

    # Special Case: Empty Graph
    if n == 0:
        return PageRankResult(
            scores=[],
            converged=True,
            iterations=0,
            final_residual=0.0,
            status=PageRankStatus.CONVERGED,
            diagnostics={"empty_graph": True}
        )

    # Special Case: Single-Node Graph (N = 1)
    if n == 1:
        return PageRankResult(
            scores=[1.0],
            converged=True,
            iterations=0,
            final_residual=0.0,
            status=PageRankStatus.CONVERGED,
            diagnostics={"single_node": True}
        )

    # Precomputation Phase
    dangling_mask, scale_factors, max_in_degree = validate_and_build_operator_scales(matrix)
    v = validate_and_normalize_personalization(personalization, n, matrix.node_keys)

    # Graph-dependent noise floor and tolerance resolution
    tau_floor = compute_theoretical_noise_floor(max_in_degree, alpha)
    clamped_tolerance = False
    effective_tol = target_atol
    if effective_tol < 2.0 * tau_floor:
        effective_tol = 2.0 * tau_floor
        clamped_tolerance = True

    # Allocate persistent solver working buffers
    p = array.array('d', v)  # Initial iterate p^(0) = v
    p_next = array.array('d', [0.0] * n)
    scaled_p_workspace = array.array('d', [0.0] * n)
    workspace_y = array.array('d', [0.0] * n)
    residual_workspace = array.array('d', [0.0] * n)

    # Telemetry tracking structures
    residual_history: List[float] = []
    acceleration_attempt_count = 0
    acceleration_success_count = 0
    acceleration_fallback_count = 0
    denominator_coordinate_fallback_count = 0
    fallback_reasons = {
        "guard_2_nonfinite": 0,
        "guard_3_negative": 0,
        "guard_4_mass": 0,
        "guard_5_normalization_failure": 0,
        "guard_6_residual_nonfinite": 0,
        "guard_7_residual_increase": 0,
    }

    # Special Case: Pure Dangling Graph (E = 0 or all nodes dangling)
    all_dangling = (matrix.nnz == 0) or all(dangling_mask[i] == 1 for i in range(n))
    if all_dangling:
        res1, res_inf = compute_production_residual(
            matrix, scale_factors, dangling_mask, p, v, alpha,
            workspace_y, scaled_p_workspace, residual_workspace
        )
        return PageRankResult(
            scores=list(p),
            converged=True,
            iterations=1,
            final_residual=res1,
            status=PageRankStatus.CONVERGED,
            diagnostics={
                "all_dangling": True,
                "initial_residual": res1,
                "final_residual": res1,
                "residual_history": [res1],
                "residual_contraction_estimate": None,
                "theoretical_noise_floor": tau_floor,
                "effective_tolerance": effective_tol,
                "tolerance_clamped_to_noise_floor": clamped_tolerance,
                "max_in_degree": max_in_degree,
                "acceleration_attempt_count": 0,
                "acceleration_success_count": 0,
                "acceleration_fallback_count": 0,
                "denominator_coordinate_fallback_count": 0,
                "fallback_reasons": fallback_reasons,
            }
        )

    # Initial residual at t = 0
    res1, res_inf = compute_production_residual(
        matrix, scale_factors, dangling_mask, p, v, alpha,
        workspace_y, scaled_p_workspace, residual_workspace
    )
    residual_history.append(res1)
    initial_res = res1

    if res1 < effective_tol:
        return PageRankResult(
            scores=list(p),
            converged=True,
            iterations=0,
            final_residual=res1,
            status=PageRankStatus.CONVERGED,
            diagnostics={
                "initial_residual": res1,
                "final_residual": res1,
                "residual_history": residual_history,
                "residual_contraction_estimate": None,
                "theoretical_noise_floor": tau_floor,
                "effective_tolerance": effective_tol,
                "tolerance_clamped_to_noise_floor": clamped_tolerance,
                "max_in_degree": max_in_degree,
                "acceleration_attempt_count": 0,
                "acceleration_success_count": 0,
                "acceleration_fallback_count": 0,
                "denominator_coordinate_fallback_count": 0,
                "fallback_reasons": fallback_reasons,
            }
        )

    # Aitken Acceleration State Machine Buffers
    aitken_state = "STATE_INIT_1"
    p_t = array.array('d', [0.0] * n)
    p_t1 = array.array('d', [0.0] * n)
    p_t2 = array.array('d', [0.0] * n)
    p_acc = array.array('d', [0.0] * n)

    iteration = 0
    converged = False
    terminal_status = PageRankStatus.MAX_ITERATIONS_REACHED

    # Main Power Iteration Loop
    while iteration < max_iter:
        iteration += 1

        if not use_acceleration:
            # Standard Unaccelerated Power Iteration
            f_p, _ = evaluate_pagerank_operator(
                matrix, scale_factors, dangling_mask, p, v, alpha,
                p_next, scaled_p_workspace
            )

            # Check for NaN / non-finite values in state
            if not all(math.isfinite(f_p[i]) for i in range(n)):
                terminal_status = PageRankStatus.NUMERICAL_OVERFLOW_OR_NAN
                break

            # Copy p_next to p
            for i in range(n):
                p[i] = p_next[i]

            # Evaluate residual
            res1, res_inf = compute_production_residual(
                matrix, scale_factors, dangling_mask, p, v, alpha,
                workspace_y, scaled_p_workspace, residual_workspace
            )
            residual_history.append(res1)

        else:
            # Aitken Acceleration 3-State Machine
            if aitken_state == "STATE_INIT_1":
                # Stencil Step 1: p_t = p, compute p_t1 = F(p_t)
                for i in range(n):
                    p_t[i] = p[i]
                evaluate_pagerank_operator(
                    matrix, scale_factors, dangling_mask, p_t, v, alpha,
                    p_t1, scaled_p_workspace
                )
                for i in range(n):
                    p[i] = p_t1[i]
                res1, res_inf = compute_production_residual(
                    matrix, scale_factors, dangling_mask, p, v, alpha,
                    workspace_y, scaled_p_workspace, residual_workspace
                )
                residual_history.append(res1)
                aitken_state = "STATE_INIT_2"

            elif aitken_state == "STATE_INIT_2":
                # Stencil Step 2: compute p_t2 = F(p_t1)
                evaluate_pagerank_operator(
                    matrix, scale_factors, dangling_mask, p_t1, v, alpha,
                    p_t2, scaled_p_workspace
                )
                for i in range(n):
                    p[i] = p_t2[i]
                res1, res_inf = compute_production_residual(
                    matrix, scale_factors, dangling_mask, p, v, alpha,
                    workspace_y, scaled_p_workspace, residual_workspace
                )
                residual_history.append(res1)
                aitken_state = "STATE_ACC_READY"

            elif aitken_state == "STATE_ACC_READY":
                # Full 3-point stencil available: (p_t, p_t1, p_t2)
                acceleration_attempt_count += 1
                candidate_accepted = False

                # Guard 1: Coordinate-level denominator check
                for i in range(n):
                    dp1 = p_t1[i] - p_t[i]
                    dp2 = p_t2[i] - p_t1[i]
                    delta2 = dp2 - dp1
                    if abs(delta2) <= 1e-14:
                        p_acc[i] = p_t2[i]
                        denominator_coordinate_fallback_count += 1
                    else:
                        p_acc[i] = p_t[i] - (dp1 * dp1) / delta2

                # Guard 2: Finite Coordinates (First-Failing-Guard Rule)
                if not all(math.isfinite(p_acc[i]) for i in range(n)):
                    acceleration_fallback_count += 1
                    fallback_reasons["guard_2_nonfinite"] += 1
                # Guard 3: Non-Negativity
                elif any(p_acc[i] < -1e-16 for i in range(n)):
                    acceleration_fallback_count += 1
                    fallback_reasons["guard_3_negative"] += 1
                else:
                    # Safe clamp
                    for i in range(n):
                        if p_acc[i] < 0.0:
                            p_acc[i] = 0.0

                    # Guard 4: Positive Mass
                    s_acc = neumaier_sum(p_acc)
                    if not math.isfinite(s_acc) or s_acc <= 1e-12:
                        acceleration_fallback_count += 1
                        fallback_reasons["guard_4_mass"] += 1
                    else:
                        # Guard 5: Simplex Normalization & Validation
                        for i in range(n):
                            p_acc[i] /= s_acc
                        post_s = neumaier_sum(p_acc)
                        if abs(post_s - 1.0) > 1e-12:
                            acceleration_fallback_count += 1
                            fallback_reasons["guard_5_normalization_failure"] += 1
                        else:
                            # Guard 6: Finite Candidate Residual
                            res_acc, _ = compute_production_residual(
                                matrix, scale_factors, dangling_mask, p_acc, v, alpha,
                                workspace_y, scaled_p_workspace, residual_workspace
                            )
                            if not math.isfinite(res_acc):
                                acceleration_fallback_count += 1
                                fallback_reasons["guard_6_residual_nonfinite"] += 1
                            else:
                                # Guard 7: Strict Residual Reduction vs unaccelerated p_t2
                                res_t2, _ = compute_production_residual(
                                    matrix, scale_factors, dangling_mask, p_t2, v, alpha,
                                    workspace_y, scaled_p_workspace, residual_workspace
                                )
                                if res_acc < res_t2:
                                    # Candidate ACCEPTED
                                    candidate_accepted = True
                                    acceleration_success_count += 1
                                    for i in range(n):
                                        p[i] = p_acc[i]
                                    res1 = res_acc
                                    residual_history.append(res1)
                                    # Reset history after acceleration adoption
                                    aitken_state = "STATE_INIT_1"
                                else:
                                    acceleration_fallback_count += 1
                                    fallback_reasons["guard_7_residual_increase"] += 1

                if not candidate_accepted:
                    # Roll history forward with standard power iteration
                    for i in range(n):
                        p_t[i] = p_t1[i]
                        p_t1[i] = p_t2[i]
                    evaluate_pagerank_operator(
                        matrix, scale_factors, dangling_mask, p_t1, v, alpha,
                        p_t2, scaled_p_workspace
                    )
                    for i in range(n):
                        p[i] = p_t2[i]
                    res1, res_inf = compute_production_residual(
                        matrix, scale_factors, dangling_mask, p, v, alpha,
                        workspace_y, scaled_p_workspace, residual_workspace
                    )
                    residual_history.append(res1)
                    aitken_state = "STATE_ACC_READY"

        # Check Stopping Criteria
        # 1. Absolute tolerance
        if res1 < effective_tol:
            converged = True
            terminal_status = PageRankStatus.CONVERGED
            break

        # 2. Relative tolerance (if specified)
        if rtol is not None and initial_res > 0.0:
            if (res1 / initial_res) < rtol:
                converged = True
                terminal_status = PageRankStatus.CONVERGED
                break

        # 3. Noise Floor Stagnation Detection
        if iteration >= 10 and res1 <= 5.0 * tau_floor:
            if len(residual_history) >= 6:
                recent = residual_history[-6:]
                rel_changes = [
                    abs(recent[k] - recent[k - 1]) / max(recent[k], 1e-18)
                    for k in range(1, 6)
                ]
                if all(rc < 1e-4 for rc in rel_changes):
                    converged = True
                    terminal_status = PageRankStatus.STAGNATED_AT_NOISE_FLOOR
                    break

        # 4. General Stagnation Detection
        if iteration >= 15 and len(residual_history) >= 11:
            recent_10 = residual_history[-11:]
            rel_changes = [
                abs(recent_10[k] - recent_10[k - 1]) / max(recent_10[k], 1e-18)
                for k in range(1, 11)
            ]
            if all(rc < 1e-5 for rc in rel_changes):
                converged = False
                terminal_status = PageRankStatus.STAGNATED
                break

    # Estimate asymptotic residual contraction rate rho
    rho_est = None
    if len(residual_history) >= 2:
        r_prev = residual_history[-2]
        r_curr = residual_history[-1]
        if r_prev > 1e-14:
            rho_est = r_curr / r_prev

    # Diagnostic metadata
    diagnostics = {
        "initial_residual": initial_res,
        "final_residual": res1,
        "residual_history": residual_history,
        "residual_contraction_estimate": rho_est,
        "theoretical_noise_floor": tau_floor,
        "effective_tolerance": effective_tol,
        "tolerance_clamped_to_noise_floor": clamped_tolerance,
        "max_in_degree": max_in_degree,
        "acceleration_attempt_count": acceleration_attempt_count,
        "acceleration_success_count": acceleration_success_count,
        "acceleration_fallback_count": acceleration_fallback_count,
        "denominator_coordinate_fallback_count": denominator_coordinate_fallback_count,
        "fallback_reasons": fallback_reasons,
    }

    return PageRankResult(
        scores=list(p),
        converged=converged,
        iterations=iteration,
        final_residual=res1,
        status=terminal_status,
        diagnostics=diagnostics
    )


# ==============================================================================
# COMPONENT-LEVEL PAGERANK & MASS BALANCE UTILITIES
# ==============================================================================

def compute_component_pagerank(
    matrix: CSRMatrix,
    component_indices: Sequence[int],
    alpha: float = 0.85,
    tol: float = 1e-12,
    max_iter: int = 200,
    personalization: Optional[Dict[str, float]] = None
) -> PageRankResult:
    """
    Computes isolated component-local PageRank (Regime C) on an induced subgraph
    of matrix defined by component_indices.
    """
    sub_keys = [matrix.node_keys[idx] for idx in component_indices]
    idx_map = {orig_idx: local_idx for local_idx, orig_idx in enumerate(component_indices)}
    n_sub = len(component_indices)

    # Sub-COO
    sub_indptr = array.array('i', [0] * (n_sub + 1))
    sub_indices_list = []
    sub_data_list = []

    for local_i, orig_i in enumerate(component_indices):
        start = matrix.indptr[orig_i]
        end = matrix.indptr[orig_i + 1]
        row_entries = []
        for k in range(start, end):
            orig_col = matrix.indices[k]
            if orig_col in idx_map:
                row_entries.append((idx_map[orig_col], matrix.data[k]))

        # Normalize component-local row if non-dangling
        row_sum = sum(val for _, val in row_entries)
        if row_sum > 0.0:
            for c_idx, val in row_entries:
                sub_indices_list.append(c_idx)
                sub_data_list.append(val / row_sum)
        sub_indptr[local_i + 1] = len(sub_data_list)

    sub_matrix = CSRMatrix(
        data=array.array('d', sub_data_list),
        indices=array.array('i', sub_indices_list),
        indptr=sub_indptr,
        shape=(n_sub, n_sub),
        node_keys=sub_keys,
        key_to_idx={k: i for i, k in enumerate(sub_keys)}
    )

    # Project personalization to component
    sub_pers: Optional[List[float]] = None
    if personalization is not None:
        sub_pers = [personalization.get(k, 0.0) for k in sub_keys]
        if sum(sub_pers) == 0.0:
            # Uniform fallback for zero-prior component
            sub_pers = [1.0 / n_sub] * n_sub

    return compute_pagerank(
        matrix=sub_matrix,
        alpha=alpha,
        tol=tol,
        max_iter=max_iter,
        personalization=sub_pers
    )
