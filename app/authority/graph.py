"""
LinkPilot AI — Phase 1B Milestone 2: Physical Graph Construction
Authoritative representation of G_observed for web authority analysis.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from app.authority.normalizer import (
    CanonicalURLNormalizer,
    NormalizedURLResult,
    SPARouteTelemetry,
    classify_domain_5tuple,
)


class VertexType(str, Enum):
    """Classification of vertices admitted to G_observed."""
    INTERNAL_CRAWLED = "internal_crawled"
    EXTERNAL_TERMINAL = "external_terminal"
    SPA_UNSCORED = "spa_unscored"


@dataclass(frozen=True)
class GraphVertex:
    """Represents a unique vertex in the physical web graph."""
    key: str  # Authoritative graph_identity_url / observed_page_key
    raw_url: str
    normalized_url: str
    domain: str
    site_boundary: str
    vertex_type: VertexType
    is_authoritative_scored_page: bool
    page_id: Optional[int] = None
    http_status: Optional[int] = None


@dataclass
class PhysicalEdgeOccurrence:
    """A single physically observed hyperlink occurrence in crawled DOM."""
    source_key: str
    target_key: str
    raw_href: str  # Tier 4 parser-decoded string
    dom_context: str = "body"
    rel_attributes: Optional[str] = None
    is_internal: bool = True


@dataclass
class AggregatedPhysicalEdge:
    """Aggregated directed edge between source and target page keys."""
    source_key: str
    target_key: str
    occurrence_count: int
    quality_factor: float  # Arithmetic mean of (rel_mult * dom_mult)
    weight: float          # Quality * diminishing returns damping
    is_self_loop: bool
    occurrences: List[PhysicalEdgeOccurrence] = field(default_factory=list)


@dataclass
class WeaklyConnectedComponent:
    """A weakly connected component (WCC) of G_observed."""
    component_id: int
    node_keys: List[str]
    node_count: int
    comp_scale: float


# ==============================================================================
# QUALITY MULTIPLIERS & WEIGHTING CONFIGURATION
# ==============================================================================

REL_MULTIPLIERS: Dict[str, float] = {
    "follow": 1.0,
    "standard": 1.0,
    "ugc": 0.5,
    "nofollow": 0.0,
    "sponsored": 0.0,
}

DOM_MULTIPLIERS: Dict[str, float] = {
    "main_content": 1.0,
    "article": 1.0,
    "body": 1.0,
    "content": 1.0,
    "header": 0.8,
    "top_navigation": 0.8,
    "nav": 0.8,
    "sidebar": 0.6,
    "aside": 0.6,
    "footer": 0.4,
    "unknown": 0.7,
}

BETA_REPETITION_DAMPING: float = 0.1
COMP_SCALE_REF_N: float = 100.0


def compute_rel_multiplier(rel_str: Optional[str]) -> float:
    """Compute rel attribute quality multiplier."""
    if not rel_str:
        return 1.0
    rel_lower = rel_str.lower()
    tokens = [t.strip() for t in rel_lower.replace(",", " ").split() if t.strip()]
    if not tokens:
        return 1.0
    # Any nofollow or sponsored zeroes the edge
    if "nofollow" in tokens or "sponsored" in tokens:
        return 0.0
    if "ugc" in tokens:
        return 0.5
    return 1.0


def compute_dom_multiplier(context_str: Optional[str]) -> float:
    """Compute DOM placement context multiplier."""
    if not context_str:
        return DOM_MULTIPLIERS["unknown"]
    ctx = context_str.strip().lower()
    return DOM_MULTIPLIERS.get(ctx, DOM_MULTIPLIERS["unknown"])


def compute_occurrence_quality(rel_str: Optional[str], context_str: Optional[str]) -> float:
    """Calculate combined quality for a single link occurrence."""
    rel_mult = compute_rel_multiplier(rel_str)
    dom_mult = compute_dom_multiplier(context_str)
    return rel_mult * dom_mult


def compute_component_scale(node_count: int, n_ref: float = COMP_SCALE_REF_N) -> float:
    """
    Component Significance Scaling from Version 1.2.2 Addendum:
    CompScale(C_k) = ln(1 + N_k) / (ln(1 + N_k) + ln(1 + N_ref))
    """
    if node_count <= 0:
        return 0.0
    ln_n = math.log(1.0 + float(node_count))
    ln_ref = math.log(1.0 + float(n_ref))
    return ln_n / (ln_n + ln_ref)


# ==============================================================================
# PHYSICAL GRAPH CONTAINER
# ==============================================================================

class PhysicalGraph:
    """
    In-memory representation of the physical web graph G_observed = (V, E).
    Enforces strict vertex partitioning, self-loop isolation, and WCC decomposition.
    """

    def __init__(self):
        self.vertices: Dict[str, GraphVertex] = {}
        # source_key -> target_key -> AggregatedPhysicalEdge
        self.edges: Dict[str, Dict[str, AggregatedPhysicalEdge]] = {}
        # Raw unaggregated occurrences buffer
        self._raw_occurrences: List[PhysicalEdgeOccurrence] = []
        self.self_loops: List[AggregatedPhysicalEdge] = []
        self._finalized: bool = False

    @property
    def total_edge_count(self) -> int:
        """Total number of directed aggregated edges across all source vertices."""
        return sum(len(target_dict) for target_dict in self.edges.values())

    def add_vertex(self, vertex: GraphVertex) -> None:
        """Add or update a vertex in the physical graph."""
        self.vertices[vertex.key] = vertex

    def add_edge_occurrence(self, occ: PhysicalEdgeOccurrence) -> None:
        """Buffer a physical edge occurrence for subsequent aggregation."""
        self._raw_occurrences.append(occ)
        self._finalized = False

    def finalize(self) -> None:
        """
        Aggregate all buffered occurrences into weighted directed edges.
        Computes occurrence-averaged quality factors and diminishing returns weights.
        """
        self.edges.clear()
        self.self_loops.clear()

        # Group occurrences by (source_key, target_key)
        grouped: Dict[Tuple[str, str], List[PhysicalEdgeOccurrence]] = {}
        for occ in self._raw_occurrences:
            pair = (occ.source_key, occ.target_key)
            if pair not in grouped:
                grouped[pair] = []
            grouped[pair].append(occ)

        for (src, tgt), occ_list in grouped.items():
            n_occ = len(occ_list)
            # Arithmetic mean of quality multipliers
            total_qual = sum(compute_occurrence_quality(o.rel_attributes, o.dom_context) for o in occ_list)
            mean_qual = total_qual / float(n_occ) if n_occ > 0 else 0.0

            # Diminishing returns formula: Q * (1 + ln(1 + beta * (N_occ - 1)))
            diminishing_factor = 1.0 + math.log(1.0 + BETA_REPETITION_DAMPING * float(n_occ - 1))
            weight = mean_qual * diminishing_factor
            is_self = (src == tgt)

            agg_edge = AggregatedPhysicalEdge(
                source_key=src,
                target_key=tgt,
                occurrence_count=n_occ,
                quality_factor=mean_qual,
                weight=weight,
                is_self_loop=is_self,
                occurrences=occ_list,
            )

            if is_self:
                self.self_loops.append(agg_edge)

            if src not in self.edges:
                self.edges[src] = {}
            self.edges[src][tgt] = agg_edge

        self._finalized = True

    def get_out_degree(self, source_key: str, exclude_self_loops: bool = True) -> float:
        """Calculate weighted out-degree for a source vertex."""
        if not self._finalized:
            self.finalize()
        if source_key not in self.edges:
            return 0.0
        deg = 0.0
        for tgt, edge in self.edges[source_key].items():
            if exclude_self_loops and edge.is_self_loop:
                continue
            deg += edge.weight
        return deg

    def get_in_degree(self, target_key: str, exclude_self_loops: bool = True) -> float:
        """Calculate weighted in-degree for a target vertex."""
        if not self._finalized:
            self.finalize()
        deg = 0.0
        for src, target_dict in self.edges.items():
            if target_key in target_dict:
                edge = target_dict[target_key]
                if exclude_self_loops and edge.is_self_loop:
                    continue
                deg += edge.weight
        return deg

    def find_weakly_connected_components(self) -> List[WeaklyConnectedComponent]:
        """
        Decompose G_observed into Weakly Connected Components (WCCs).
        Uses BFS over the symmetrized undirected graph projection.
        """
        if not self._finalized:
            self.finalize()

        # Build undirected adjacency map
        adj: Dict[str, Set[str]] = {k: set() for k in self.vertices}
        for src, target_dict in self.edges.items():
            for tgt, edge in target_dict.items():
                if edge.is_self_loop:
                    continue
                if src in adj and tgt in adj:
                    adj[src].add(tgt)
                    adj[tgt].add(src)

        visited: Set[str] = set()
        components: List[WeaklyConnectedComponent] = []
        comp_id = 0

        # Deterministic sorting of node keys for reproducibility
        sorted_keys = sorted(self.vertices.keys())

        for node in sorted_keys:
            if node in visited:
                continue
            # BFS queue
            queue = [node]
            visited.add(node)
            comp_nodes: List[str] = []

            head = 0
            while head < len(queue):
                curr = queue[head]
                head += 1
                comp_nodes.append(curr)

                for neighbor in sorted(adj.get(curr, set())):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)

            comp_nodes.sort()
            n_k = len(comp_nodes)
            scale = compute_component_scale(n_k)

            components.append(
                WeaklyConnectedComponent(
                    component_id=comp_id,
                    node_keys=comp_nodes,
                    node_count=n_k,
                    comp_scale=scale,
                )
            )
            comp_id += 1

        # Sort components descending by size
        components.sort(key=lambda c: c.node_count, reverse=True)
        # Re-index component IDs deterministically
        for i, c in enumerate(components):
            c.component_id = i

        return components

    def get_subgraph(self, component: WeaklyConnectedComponent) -> "PhysicalGraph":
        """Extract a sub-graph corresponding strictly to a single WCC."""
        if not self._finalized:
            self.finalize()

        sub = PhysicalGraph()
        comp_set = set(component.node_keys)

        for k in component.node_keys:
            if k in self.vertices:
                sub.add_vertex(self.vertices[k])

        for occ in self._raw_occurrences:
            if occ.source_key in comp_set and occ.target_key in comp_set:
                sub.add_edge_occurrence(occ)

        sub.finalize()
        return sub


# ==============================================================================
# DATABASE EXTRACTOR & GRAPH BUILDER
# ==============================================================================

def build_observed_graph_from_db(
    db_path: str = "./data/linkpilot.db",
    require_read_only: bool = True
) -> PhysicalGraph:
    """
    Construct G_observed directly from SQLite database in read-only mode.
    Queries 'pages', 'page_links', and 'link_occurrences' without mutating schema.
    """
    uri = f"file:{db_path}?mode=ro" if require_read_only else db_path
    conn = sqlite3.connect(uri, uri=require_read_only)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    graph = PhysicalGraph()

    # Map of page_id -> GraphVertex
    page_id_to_vertex: Dict[int, GraphVertex] = {}
    # Map of page_id -> primary normalized URL
    page_id_to_norm_url: Dict[int, str] = {}

    try:
        # 1. Ingest internal crawled pages
        cursor.execute("""
            SELECT id, raw_url, normalized_url, http_status
            FROM pages
        """)
        for row in cursor.fetchall():
            pid = row["id"]
            raw_url = row["raw_url"] or ""
            norm_url_raw = row["normalized_url"] or raw_url

            norm_res = CanonicalURLNormalizer.normalize_url(norm_url_raw)
            if not norm_res.is_valid_web_url:
                continue

            page_key = norm_res.observed_page_key or norm_res.normalized_url

            # Determine vertex type
            v_type = VertexType.INTERNAL_CRAWLED
            is_scored = True
            if norm_res.spa_telemetry in (
                SPARouteTelemetry.HASH_ROUTE_SUSPECTED,
                SPARouteTelemetry.FRAGMENT_ROUTING_UNKNOWN,
            ):
                v_type = VertexType.SPA_UNSCORED
                is_scored = False

            vertex = GraphVertex(
                key=page_key,
                raw_url=raw_url,
                normalized_url=norm_res.normalized_url,
                domain=norm_res.domain.registered_domain,
                site_boundary=norm_res.domain.site_boundary,
                vertex_type=v_type,
                is_authoritative_scored_page=is_scored,
                page_id=pid,
                http_status=row["http_status"],
            )

            graph.add_vertex(vertex)
            page_id_to_vertex[pid] = vertex
            page_id_to_norm_url[pid] = norm_res.normalized_url

        # 2. Ingest link occurrences & build edges
        cursor.execute("""
            SELECT 
                lo.id,
                lo.page_link_id,
                lo.raw_target_url,
                lo.normalized_target_url,
                lo.rel_attributes,
                lo.dom_context,
                pl.source_page_id,
                pl.target_page_id
            FROM link_occurrences lo
            LEFT JOIN page_links pl ON lo.page_link_id = pl.id
        """)

        for row in cursor.fetchall():
            src_pid = row["source_page_id"]
            if not src_pid or src_pid not in page_id_to_vertex:
                continue

            src_vertex = page_id_to_vertex[src_pid]
            base_url = src_vertex.normalized_url

            raw_target = row["raw_target_url"] or row["normalized_target_url"] or ""
            if not raw_target:
                continue

            # Normalize target relative to source base URL
            tgt_res = CanonicalURLNormalizer.normalize_url(raw_target, base_url=base_url)
            if not tgt_res.is_valid_web_url:
                continue

            tgt_key = tgt_res.observed_page_key or tgt_res.normalized_url

            # Check if target exists in graph, else admit as external terminal sink
            if tgt_key not in graph.vertices:
                is_internal = (tgt_res.domain.site_boundary == src_vertex.site_boundary)
                v_type = VertexType.INTERNAL_CRAWLED if is_internal else VertexType.EXTERNAL_TERMINAL
                # Third-party discovered targets are unscored
                is_scored = False

                tgt_vertex = GraphVertex(
                    key=tgt_key,
                    raw_url=raw_target,
                    normalized_url=tgt_res.normalized_url,
                    domain=tgt_res.domain.registered_domain,
                    site_boundary=tgt_res.domain.site_boundary,
                    vertex_type=v_type,
                    is_authoritative_scored_page=is_scored,
                    page_id=row["target_page_id"],
                )
                graph.add_vertex(tgt_vertex)
            else:
                tgt_vertex = graph.vertices[tgt_key]
                is_internal = (tgt_vertex.site_boundary == src_vertex.site_boundary)

            occ = PhysicalEdgeOccurrence(
                source_key=src_vertex.key,
                target_key=tgt_key,
                raw_href=raw_target,
                dom_context=row["dom_context"] or "body",
                rel_attributes=row["rel_attributes"],
                is_internal=is_internal,
            )
            graph.add_edge_occurrence(occ)

    finally:
        conn.close()

    graph.finalize()
    return graph
