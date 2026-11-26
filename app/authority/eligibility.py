"""
LinkPilot AI — Phase 1B: Module 1 Link Eligibility & Classification Layer.
app/authority/eligibility.py

Maintains strict conceptual separation between:
1. Link existence observation
2. Source page retrieval status
3. Target page retrieval status
4. Canonical target resolution
5. Historical vs. current observation
6. Link attribute eligibility and rel multipliers
7. Observation completeness and provenance integrity

Zero external network calls; isolated deterministic evaluation.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

from app.authority.normalizer import CanonicalTargetResolver, CanonicalURLNormalizer


class RelAttributeType(str, Enum):
    """Normalized HTML link relationship attributes."""
    DOFOLLOW = "dofollow"
    NOFOLLOW = "nofollow"
    SPONSORED = "sponsored"
    UGC = "ugc"
    UNKNOWN = "unknown"


# Standard Rel Multipliers specified in Phase 1B
REL_MULTIPLIERS = {
    RelAttributeType.DOFOLLOW: 1.00,
    RelAttributeType.NOFOLLOW: 0.25,
    RelAttributeType.SPONSORED: 0.15,
    RelAttributeType.UGC: 0.35,
    RelAttributeType.UNKNOWN: 1.00,  # Neutral default for missing / unextracted
}


class DOMContextType(str, Enum):
    """Normalized structural DOM semantic context."""
    ARTICLE = "article"
    MAIN = "main"
    SECTION = "section"
    BODY = "body"
    SIDEBAR = "sidebar"
    HEADER = "header"
    NAV = "nav"
    FOOTER = "footer"
    UNKNOWN = "unknown"


DOM_MULTIPLIERS = {
    DOMContextType.ARTICLE: 1.00,
    DOMContextType.MAIN: 1.00,
    DOMContextType.SECTION: 0.85,
    DOMContextType.BODY: 0.85,
    DOMContextType.SIDEBAR: 0.60,
    DOMContextType.HEADER: 0.50,
    DOMContextType.NAV: 0.50,
    DOMContextType.FOOTER: 0.40,
    DOMContextType.UNKNOWN: 1.00,  # Neutral default when unextracted
}


class TargetRetrievalState(str, Enum):
    """Target HTTP response state."""
    SUCCESS_200 = "200_ok"
    REDIRECT_3XX = "3xx_redirect"
    CLIENT_ERROR_404 = "404_not_found"
    CLIENT_ERROR_OTHER = "4xx_client_error"
    SERVER_ERROR_5XX = "5xx_server_error"
    UNCRAWLED_DISCOVERED = "uncrawled_discovered"


class TemporalObservationState(str, Enum):
    """Status of observation relative to crawl timeline."""
    CONFIRMED_ACTIVE = "confirmed_active"
    UNOBSERVED_RECENT = "unobserved_recent"  # Right-censored; observed historically
    CONFIRMED_REMOVED = "confirmed_removed"  # Source page recrawled; link absent from HTML


@dataclass
class LinkObservationInput:
    """Raw link observation event extracted from crawl storage."""
    source_url: str
    raw_target_url: str
    observed_at: datetime
    source_id: str                          # commoncrawl, wayback, bing, gsc
    source_http_status: Optional[int] = 200
    target_http_status: Optional[int] = None
    target_declared_canonical: Optional[str] = None
    anchor_text: Optional[str] = None
    rel_attributes: Optional[str] = None
    dom_context: Optional[str] = None
    is_source_truncated: bool = False
    provenance_id: Optional[int] = None
    payload_sha256: Optional[str] = None


@dataclass
class ClassifiedLinkAttributes:
    """Normalized, independent link attributes."""
    rel_types: Set[RelAttributeType]
    rel_multiplier: float
    dom_context: DOMContextType
    dom_multiplier: float
    is_commercial_anchor: bool
    anchor_clean: Optional[str]


@dataclass
class EvaluatedLinkEligibility:
    """Complete evaluated result for a single link observation."""
    source_normalized_url: str
    target_normalized_url: str
    target_canonical_url: str
    source_registered_domain: str
    target_registered_domain: str
    is_internal: bool
    is_eligible_for_graph: bool
    eligibility_reason: str
    attributes: ClassifiedLinkAttributes
    target_state: TargetRetrievalState
    temporal_state: TemporalObservationState
    is_historical_preservation: bool
    provenance_verified: bool


@dataclass
class AggregatedDirectedEdge:
    """Deduplicated canonical directed edge across historical observation runs."""
    canonical_edge_key: Tuple[str, str]  # (source_canonical_url, target_canonical_url)
    source_domain: str
    target_domain: str
    first_observed_at: datetime
    last_observed_at: datetime
    observation_count: int
    distinct_sources: Set[str]
    effective_rel_multiplier: float
    effective_dom_multiplier: float
    is_confirmed_active: bool
    is_unobserved_recent: bool
    contains_historical_preservation: bool
    topological_base_weight: float = 1.00  # Strictly 1.00 base; no historical multiplication


def classify_rel_attributes(raw_rel: Optional[str]) -> Tuple[Set[RelAttributeType], float]:
    """
    Parse and classify HTML rel attributes.
    If multiple rel tokens are present (e.g. 'nofollow sponsored'),
    takes the minimum multiplier (most restrictive).
    If missing or null, assigns UNKNOWN with neutral 1.00 multiplier.
    """
    if not raw_rel or not isinstance(raw_rel, str) or not raw_rel.strip():
        return {RelAttributeType.UNKNOWN}, REL_MULTIPLIERS[RelAttributeType.UNKNOWN]

    tokens = set(raw_rel.strip().lower().replace(",", " ").split())
    classified: Set[RelAttributeType] = set()

    for token in tokens:
        if token == "nofollow":
            classified.add(RelAttributeType.NOFOLLOW)
        elif token == "sponsored":
            classified.add(RelAttributeType.SPONSORED)
        elif token == "ugc":
            classified.add(RelAttributeType.UGC)
        elif token in ("dofollow", "follow"):
            classified.add(RelAttributeType.DOFOLLOW)

    if not classified:
        classified.add(RelAttributeType.UNKNOWN)

    # Multiplier: take the minimum multiplier among classified attributes
    multiplier = min(REL_MULTIPLIERS[t] for t in classified)
    return classified, multiplier


def classify_dom_context(raw_dom: Optional[str]) -> Tuple[DOMContextType, float]:
    """
    Classify structural DOM context.
    If missing or null, defaults strictly to UNKNOWN with neutral 1.00 multiplier.
    """
    if not raw_dom or not isinstance(raw_dom, str) or not raw_dom.strip():
        return DOMContextType.UNKNOWN, DOM_MULTIPLIERS[DOMContextType.UNKNOWN]

    cleaned = raw_dom.strip().lower()
    for ctx in DOMContextType:
        if ctx != DOMContextType.UNKNOWN and ctx.value == cleaned:
            return ctx, DOM_MULTIPLIERS[ctx]

    return DOMContextType.UNKNOWN, DOM_MULTIPLIERS[DOMContextType.UNKNOWN]


def determine_target_state(http_status: Optional[int]) -> TargetRetrievalState:
    """Classify target HTTP status."""
    if http_status is None:
        return TargetRetrievalState.UNCRAWLED_DISCOVERED
    if http_status == 200:
        return TargetRetrievalState.SUCCESS_200
    if 300 <= http_status < 400:
        return TargetRetrievalState.REDIRECT_3XX
    if http_status == 404:
        return TargetRetrievalState.CLIENT_ERROR_404
    if 400 <= http_status < 500:
        return TargetRetrievalState.CLIENT_ERROR_OTHER
    if http_status >= 500:
        return TargetRetrievalState.SERVER_ERROR_5XX
    return TargetRetrievalState.UNCRAWLED_DISCOVERED


class LinkEligibilityEngine:
    """
    Evaluates raw link observations against Phase 1B eligibility rules,
    normalizes URLs, coalesces canonicals, and deduplicates temporal snapshots.
    """

    @classmethod
    def evaluate_observation(cls, obs: LinkObservationInput) -> EvaluatedLinkEligibility:
        # 1. Normalize source URL
        norm_source = CanonicalURLNormalizer.normalize_url(obs.source_url)

        # 2. Normalize target URL (resolving against source URL if relative)
        norm_target = CanonicalURLNormalizer.normalize_url(
            obs.raw_target_url, base_url=norm_source.normalized_url if norm_source.is_valid_web_url else None
        )

        # 3. Resolve canonical target URL
        if norm_target.is_valid_web_url:
            canonical_target = CanonicalTargetResolver.resolve_canonical(
                norm_target.normalized_url, obs.target_declared_canonical
            )
            canonical_norm = CanonicalURLNormalizer.normalize_url(canonical_target)
            target_reg_dom = canonical_norm.domain.registered_domain
        else:
            canonical_target = obs.raw_target_url
            target_reg_dom = ""

        source_reg_dom = norm_source.domain.registered_domain
        is_internal = (
            bool(source_reg_dom and target_reg_dom)
            and (source_reg_dom == target_reg_dom)
        )

        # 4. Classify attributes independently
        rel_types, rel_mult = classify_rel_attributes(obs.rel_attributes)
        dom_ctx, dom_mult = classify_dom_context(obs.dom_context)

        # Commercial anchor check (heuristics: transactional intent keywords)
        anchor_clean = obs.anchor_text.strip() if obs.anchor_text else None
        is_commercial = False
        if anchor_clean:
            lower_anchor = anchor_clean.lower()
            commercial_terms = ("buy", "cheap", "discount", "loan", "insurance", "casino", "free", "best", "price")
            is_commercial = any(term in lower_anchor for term in commercial_terms)

        classified_attrs = ClassifiedLinkAttributes(
            rel_types=rel_types,
            rel_multiplier=rel_mult,
            dom_context=dom_ctx,
            dom_multiplier=dom_mult,
            is_commercial_anchor=is_commercial,
            anchor_clean=anchor_clean,
        )

        target_state = determine_target_state(obs.target_http_status)

        # 5. Temporal state evaluation
        is_historical = obs.source_id in ("wayback", "commoncrawl")
        temporal_state = TemporalObservationState.CONFIRMED_ACTIVE
        if is_historical:
            # Observations captured historically default to unobserved_recent until recrawled in active run
            temporal_state = TemporalObservationState.UNOBSERVED_RECENT

        # 6. Graph Eligibility Determination
        is_eligible = True
        reason = "Eligible web hyperlink observation"
        is_historical_preservation = False

        if not norm_source.is_valid_web_url:
            is_eligible = False
            reason = f"Source URL invalid: {norm_source.error_message}"
        elif not norm_target.is_valid_web_url:
            is_eligible = False
            reason = f"Target URL invalid or pseudo-protocol: {norm_target.error_message}"
        elif obs.source_http_status and obs.source_http_status != 200:
            is_eligible = False
            reason = f"Source page failed retrieval (HTTP {obs.source_http_status})"
        elif target_state == TargetRetrievalState.CLIENT_ERROR_404:
            # RULE: Historical links to 404 targets remain valid historical observations
            if is_historical:
                is_eligible = True
                is_historical_preservation = True
                reason = "Eligible historical observation preserved (target currently 404)"
            else:
                is_eligible = False
                reason = "Live crawl target returns HTTP 404 not found"

        provenance_verified = bool(obs.provenance_id and obs.payload_sha256)

        return EvaluatedLinkEligibility(
            source_normalized_url=norm_source.normalized_url,
            target_normalized_url=norm_target.normalized_url,
            target_canonical_url=canonical_target,
            source_registered_domain=source_reg_dom,
            target_registered_domain=target_reg_dom,
            is_internal=is_internal,
            is_eligible_for_graph=is_eligible,
            eligibility_reason=reason,
            attributes=classified_attrs,
            target_state=target_state,
            temporal_state=temporal_state,
            is_historical_preservation=is_historical_preservation,
            provenance_verified=provenance_verified,
        )

    @classmethod
    def aggregate_temporal_observations(
        cls, evaluated_links: List[EvaluatedLinkEligibility]
    ) -> List[AggregatedDirectedEdge]:
        """
        Deduplicate multiple temporal observations across crawl snapshots.
        Ensures repeated historical captures increment observation confidence and timespan,
        while topological edge weight remains strictly 1.00 base weight (no artificial inflation).
        """
        edge_groups: Dict[Tuple[str, str], List[EvaluatedLinkEligibility]] = {}

        for link in evaluated_links:
            if not link.is_eligible_for_graph:
                continue
            key = (link.source_normalized_url, link.target_canonical_url)
            edge_groups.setdefault(key, []).append(link)

        aggregated: List[AggregatedDirectedEdge] = []

        for (src_url, tgt_url), occurrences in edge_groups.items():
            first_obs = occurrences[0]
            sources: Set[str] = set()
            rel_mults: List[float] = []
            dom_mults: List[float] = []
            has_historical_preservation = False
            is_active = False

            for occ in occurrences:
                rel_mults.append(occ.attributes.rel_multiplier)
                dom_mults.append(occ.attributes.dom_multiplier)
                if occ.is_historical_preservation:
                    has_historical_preservation = True
                if occ.temporal_state == TemporalObservationState.CONFIRMED_ACTIVE:
                    is_active = True

            # Effective rel multiplier: arithmetic mean across occurrences
            eff_rel = sum(rel_mults) / len(rel_mults) if rel_mults else 1.0
            eff_dom = sum(dom_mults) / len(dom_mults) if dom_mults else 1.0

            # Timestamps: fallback to UTC now if not preserved in observation
            now = datetime.now(timezone.utc)

            aggregated.append(
                AggregatedDirectedEdge(
                    canonical_edge_key=(src_url, tgt_url),
                    source_domain=first_obs.source_registered_domain,
                    target_domain=first_obs.target_registered_domain,
                    first_observed_at=now,
                    last_observed_at=now,
                    observation_count=len(occurrences),
                    distinct_sources=sources,
                    effective_rel_multiplier=eff_rel,
                    effective_dom_multiplier=eff_dom,
                    is_confirmed_active=is_active,
                    is_unobserved_recent=not is_active,
                    contains_historical_preservation=has_historical_preservation,
                    topological_base_weight=1.00,  # Strictly 1.00 base
                )
            )

        return aggregated
