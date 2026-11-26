"""
LinkPilot AI — Phase 1B Milestone 2: Transition Matrix Assembly
High-performance Compressed Sparse Row (CSR) matrix construction and PageRank orientation.
Built strictly on Python standard library (array.array) with zero external package dependencies.
"""

from __future__ import annotations

import array
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from app.authority.graph import PhysicalGraph, WeaklyConnectedComponent


@dataclass
class CSRMatrix:
    """
    SciPy-compatible Compressed Sparse Row (CSR) representation.
    Memory-bounded:
      data: array.array('d') (float64, 8 bytes per non-zero)
      indices: array.array('i') (int32, 4 bytes per non-zero)
      indptr: array.array('i') (int32, 4 bytes per row, length N + 1)
    """
    data: array.array
    indices: array.array
    indptr: array.array
    shape: Tuple[int, int]
    node_keys: List[str]
    key_to_idx: Dict[str, int]

    @property
    def nrows(self) -> int:
        return self.shape[0]

    @property
    def ncols(self) -> int:
        return self.shape[1]

    @property
    def nnz(self) -> int:
        return len(self.data)

    def memory_bytes(self) -> int:
        """Calculate exact in-memory byte size of the 3 underlying contiguous C arrays."""
        data_bytes = len(self.data) * self.data.itemsize
        indices_bytes = len(self.indices) * self.indices.itemsize
        indptr_bytes = len(self.indptr) * self.indptr.itemsize
        return data_bytes + indices_bytes + indptr_bytes

    def get_row(self, row_idx: int) -> List[Tuple[int, float]]:
        """Return non-zero (col_idx, value) pairs for a specific row."""
        start = self.indptr[row_idx]
        end = self.indptr[row_idx + 1]
        return [(self.indices[i], self.data[i]) for i in range(start, end)]

    def to_dense(self) -> List[List[float]]:
        """Convert to dense 2D list for inspection and test verification."""
        nrows, ncols = self.shape
        dense = [[0.0] * ncols for _ in range(nrows)]
        for r in range(nrows):
            start = self.indptr[r]
            end = self.indptr[r + 1]
            for i in range(start, end):
                c = self.indices[i]
                v = self.data[i]
                dense[r][c] = v
        return dense

    def matvec(self, x: List[float]) -> List[float]:
        """
        Matrix-vector multiplication: y = M * x
        y[i] = sum_j (M[i, j] * x[j])
        """
        nrows, ncols = self.shape
        if len(x) != ncols:
            raise ValueError(f"Vector length {len(x)} must match matrix ncols {ncols}")
        y = [0.0] * nrows
        for i in range(nrows):
            start = self.indptr[i]
            end = self.indptr[i + 1]
            row_sum = 0.0
            for k in range(start, end):
                col = self.indices[k]
                row_sum += self.data[k] * x[col]
            y[i] = row_sum
        return y

    def matvec_transpose(self, x: List[float]) -> List[float]:
        """
        Matrix-transpose vector multiplication: y = M^T * x
        y[j] = sum_i (M[i, j] * x[i])
        Probability flows from row i to column j.
        """
        nrows, ncols = self.shape
        if len(x) != nrows:
            raise ValueError(f"Vector length {len(x)} must match matrix nrows {nrows}")
        y = [0.0] * ncols
        for i in range(nrows):
            x_i = x[i]
            if x_i == 0.0:
                continue
            start = self.indptr[i]
            end = self.indptr[i + 1]
            for k in range(start, end):
                col = self.indices[k]
                y[col] += self.data[k] * x_i
        return y

    def transpose(self) -> "CSRMatrix":
        """
        Compute M^T in CSR format using the two-pass linear-time transpose algorithm.
        Time Complexity: O(nrows + ncols + nnz)
        """
        nrows, ncols = self.shape
        nnz = self.nnz

        # Pass 1: Count nonzeros per column of M (which become rows of M^T)
        col_counts = [0] * ncols
        for c in self.indices:
            col_counts[c] += 1

        # Pass 2: Build indptr for M^T
        type_code = 'i' if (nrows < 2147483647 and nnz < 2147483647) else 'q'
        t_indptr = array.array(type_code, [0] * (ncols + 1))
        for j in range(ncols):
            t_indptr[j + 1] = t_indptr[j] + col_counts[j]

        # Allocate arrays for M^T
        t_data = array.array('d', [0.0] * nnz)
        t_indices = array.array(type_code, [0] * nnz)

        # Work counter to track next insertion position in each row of M^T
        head_ptrs = array.array(type_code, t_indptr[:-1])

        # Pass 3: Scatter entries from M into M^T
        for i in range(nrows):
            start = self.indptr[i]
            end = self.indptr[i + 1]
            for k in range(start, end):
                j = self.indices[k]
                val = self.data[k]
                dest_pos = head_ptrs[j]
                t_indices[dest_pos] = i
                t_data[dest_pos] = val
                head_ptrs[j] += 1

        return CSRMatrix(
            data=t_data,
            indices=t_indices,
            indptr=t_indptr,
            shape=(ncols, nrows),
            node_keys=self.node_keys,
            key_to_idx=self.key_to_idx,
        )


# ==============================================================================
# CSR MATRIX BUILDER
# ==============================================================================

class CSRMatrixBuilder:
    """
    Constructs row-stochastic CSR transition matrices from PhysicalGraph.
    Enforces row-stochastic source-to-target convention, self-loop zeroing,
    and automatic int32/int64 index type promotion.
    """

    @staticmethod
    def build_transition_matrix(
        graph: PhysicalGraph,
        component: Optional[WeaklyConnectedComponent] = None,
        exclude_self_loops: bool = True
    ) -> CSRMatrix:
        """
        Assemble the row-stochastic transition matrix M for a graph or WCC.
        Rows represent source pages; columns represent target pages.
        M[i, j] = A[i, j] / D_out(i).
        If D_out(i) == 0, row i contains all zeroes (dangling node).
        """
        # Determine vertex set
        if component is not None:
            node_keys = list(component.node_keys)
        else:
            node_keys = sorted(graph.vertices.keys())

        n = len(node_keys)
        key_to_idx = {k: idx for idx, k in enumerate(node_keys)}
        node_set = set(node_keys)

        # Temporary COO triplets
        coo_rows: List[int] = []
        coo_cols: List[int] = []
        coo_data: List[float] = []

        for i, src_key in enumerate(node_keys):
            target_edges = graph.edges.get(src_key, {})
            # Filter targets to those in the same vertex set
            valid_targets: List[Tuple[int, float]] = []
            for tgt_key, edge in target_edges.items():
                if tgt_key not in node_set:
                    continue
                if exclude_self_loops and edge.is_self_loop:
                    continue
                if edge.weight > 0.0:
                    valid_targets.append((key_to_idx[tgt_key], edge.weight))

            d_out = sum(w for _, w in valid_targets)
            if d_out > 0.0:
                # Normalize row to sum to 1.0 (row-stochastic)
                for tgt_idx, weight in valid_targets:
                    coo_rows.append(i)
                    coo_cols.append(tgt_idx)
                    coo_data.append(weight / d_out)

        nnz = len(coo_data)

        # Determine index type: int32 safe up to 2^31 - 1
        type_code = 'i' if (n < 2147483647 and nnz < 2147483647) else 'q'

        # Convert COO to CSR
        # 1. Count row nonzeros
        row_counts = [0] * n
        for r in coo_rows:
            row_counts[r] += 1

        # 2. Build indptr
        indptr = array.array(type_code, [0] * (n + 1))
        for i in range(n):
            indptr[i + 1] = indptr[i] + row_counts[i]

        # 3. Populate indices and data
        indices = array.array(type_code, [0] * nnz)
        data = array.array('d', [0.0] * nnz)

        # Pointer tracker per row
        head_ptrs = array.array(type_code, indptr[:-1])
        for r, c, val in zip(coo_rows, coo_cols, coo_data):
            pos = head_ptrs[r]
            indices[pos] = c
            data[pos] = val
            head_ptrs[r] += 1

        return CSRMatrix(
            data=data,
            indices=indices,
            indptr=indptr,
            shape=(n, n),
            node_keys=node_keys,
            key_to_idx=key_to_idx,
        )


# ==============================================================================
# PAGERANK COMPUTATION & SCORING UTILITIES
# ==============================================================================

def identify_dangling_nodes(matrix: CSRMatrix) -> List[int]:
    """Identify indices of dangling nodes (nodes with out-degree zero)."""
    dangling: List[int] = []
    for i in range(matrix.nrows):
        if matrix.indptr[i + 1] == matrix.indptr[i]:
            dangling.append(i)
    return dangling


def compute_stationary_distribution(
    matrix: CSRMatrix,
    alpha: float = 0.85,
    max_iter: int = 200,
    tol: float = 1e-12
) -> List[float]:
    """
    Compute stationary PageRank distribution using the Power Iteration method.
    Correctly applies the matrix transpose M^T for row-stochastic M:
      p = alpha * M^T * p + ((1 - alpha + alpha * P_dangle) / N) * e
    """
    n = matrix.nrows
    if n == 0:
        return []
    if n == 1:
        return [1.0]

    # Initialize uniform probability distribution
    p = [1.0 / float(n)] * n
    dangling_indices = identify_dangling_nodes(matrix)

    for iteration in range(max_iter):
        # 1. Total dangling mass
        p_dangle = sum(p[idx] for idx in dangling_indices)

        # 2. Transition step along directed hyperlinks: y = M^T * p
        y = matrix.matvec_transpose(p)

        # 3. Teleportation & dangling redistribution baseline
        teleport_mass = (1.0 - alpha + alpha * p_dangle) / float(n)

        # 4. Assemble new probability vector
        p_next = [alpha * y_j + teleport_mass for y_j in y]

        # 5. Check L1 convergence: sum |p_next - p|
        l1_diff = sum(abs(p_next[i] - p[i]) for i in range(n))
        p = p_next

        if l1_diff < tol:
            break

    # Final normalization to ensure sum(p) == 1.0
    total = sum(p)
    if total > 0.0:
        p = [x / total for x in p]

    return p


def compute_page_authority_scores(
    matrix: CSRMatrix,
    stationary_dist: List[float],
    comp_scale: float,
    alpha: float = 0.85
) -> Dict[str, float]:
    """
    Map stationary PageRank distribution to final LinkPilot Page Authority (LP-PA) scores.
    Implements Version 1.2.2 Addendum formulas:
      p_base = (1 - alpha + alpha * P_dangle) / N
      p_directed = max(0.0, (p - p_base) / alpha)
      ScaledEquity = p_directed * CompScale * 100.0
      Score = min(1.0, max(0.0, log10(1 + ScaledEquity) / log10(51.0)))
    """
    n = matrix.nrows
    if n == 0:
        return {}

    dangling_indices = identify_dangling_nodes(matrix)
    p_dangle = sum(stationary_dist[idx] for idx in dangling_indices)
    p_base = (1.0 - alpha + alpha * p_dangle) / float(n)

    log51 = math.log10(51.0)
    scores: Dict[str, float] = {}

    for i, key in enumerate(matrix.node_keys):
        p_i = stationary_dist[i]
        p_directed = max(0.0, (p_i - p_base) / alpha)
        scaled_equity = p_directed * comp_scale * 100.0
        score = math.log10(1.0 + scaled_equity) / log51
        score = min(1.0, max(0.0, score))
        scores[key] = score

    return scores


def calculate_expected_csr_memory(n_vertices: int, n_edges: int) -> Dict[str, int]:
    """
    Quantitative memory verification helper:
    CSR Memory = 12 * n_edges + 4 * n_vertices + 4
    Peak Memory = 28 * n_edges + 4 * n_vertices + 4
    """
    csr_bytes = (12 * n_edges) + (4 * n_vertices) + 4
    peak_bytes = (28 * n_edges) + (4 * n_vertices) + 4
    return {
        "csr_bytes": csr_bytes,
        "peak_assembly_bytes": peak_bytes,
        "csr_mb": csr_bytes / (1024 * 1024),
        "peak_mb": peak_bytes / (1024 * 1024),
    }
