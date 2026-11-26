"""
LinkPilot AI — Phase 1B Milestone 5: Domain Authority Aggregation & Scoring Engine (LP-DA).
Implements the LP-DA Weighted Authority Aggregation Model v1:
  LP-DA(D) = clamp(alpha * A(D) + beta * T_k(D) + gamma * B(D) * M(D), 0.0, 1.0)
Zero external package dependencies beyond LinkPilot standard stack.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

from app.authority.normalizer import CanonicalURLNormalizer, classify_domain_5tuple

# ==============================================================================
# MODEL CONSTANTS & PROVISIONAL BASELINE DEFAULTS (LP-DA v1.0.0)
# ==============================================================================

DEFAULT_DA_ALPHA: float = 0.40
DEFAULT_DA_BETA: float = 0.35
DEFAULT_DA_GAMMA: float = 0.25
DEFAULT_DA_TOP_K: int = 5
DEFAULT_DA_EPSILON: float = 1e-4
DEFAULT_DA_REF_N: float = 100.0
DA_ALGORITHM_VERSION: str = "LP-DA-v1.0.0"


# ==============================================================================
# DATA CONTRACTS
# ==============================================================================

@dataclass(frozen=True)
class DomainAuthorityComputation:
    """Immutable mathematical result of a single domain authority calculation."""
    domain_name: str
    lp_da_score: float
    lp_da_display: float
    anchor_page_pa: float
    top_k_harmonic_pa: float
    mean_page_pa: float
    breadth_score: float
    scored_pages_count: int
    configured_top_k: int = DEFAULT_DA_TOP_K
    harmonic_epsilon: float = DEFAULT_DA_EPSILON
    reference_page_count: float = DEFAULT_DA_REF_N
    alpha: float = DEFAULT_DA_ALPHA
    beta: float = DEFAULT_DA_BETA
    gamma: float = DEFAULT_DA_GAMMA
    algorithm_version: str = DA_ALGORITHM_VERSION
    top_page_urls: List[str] = field(default_factory=list)

    def to_record_dict(
        self,
        domain_id: Optional[int] = None,
        computed_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Convert computation result into an ORM-compatible dictionary for DomainAuthorityScore."""
        return {
            "domain_id": domain_id,
            "domain_name": self.domain_name,
            "lp_da_score": self.lp_da_score,
            "lp_da_display": self.lp_da_display,
            "anchor_page_pa": self.anchor_page_pa,
            "top_k_harmonic_pa": self.top_k_harmonic_pa,
            "mean_page_pa": self.mean_page_pa,
            "breadth_score": self.breadth_score,
            "scored_pages_count": self.scored_pages_count,
            "configured_top_k": self.configured_top_k,
            "harmonic_epsilon": self.harmonic_epsilon,
            "reference_page_count": self.reference_page_count,
            "alpha": self.alpha,
            "beta": self.beta,
            "gamma": self.gamma,
            "algorithm_version": self.algorithm_version,
            "computed_at": computed_at or datetime.now(timezone.utc),
        }


# ==============================================================================
# VALIDATION & CANONICALIZATION HELPERS
# ==============================================================================

def validate_da_parameters(
    alpha: float = DEFAULT_DA_ALPHA,
    beta: float = DEFAULT_DA_BETA,
    gamma: float = DEFAULT_DA_GAMMA,
    top_k: int = DEFAULT_DA_TOP_K,
    epsilon: float = DEFAULT_DA_EPSILON,
    reference_page_count: float = DEFAULT_DA_REF_N,
) -> None:
    """Validate LP-DA model weights and hyperparameters."""
    for name, val in (("alpha", alpha), ("beta", beta), ("gamma", gamma)):
        if val is None or isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(float(val)):
            raise ValueError(f"Weight {name} must be a finite number, got {val!r}")
        if not (0.0 <= float(val) <= 1.0):
            raise ValueError(f"Weight {name} must be in [0.0, 1.0], got {val}")

    weight_sum = float(alpha) + float(beta) + float(gamma)
    if not math.isclose(weight_sum, 1.0, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError(
            f"DA weights alpha + beta + gamma must sum to 1.0, got {weight_sum:.12f} "
            f"(alpha={alpha}, beta={beta}, gamma={gamma})"
        )

    if top_k is None or isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError(f"configured_top_k must be an integer >= 1, got {top_k!r}")

    if (
        epsilon is None
        or isinstance(epsilon, bool)
        or not isinstance(epsilon, (int, float))
        or not math.isfinite(float(epsilon))
        or float(epsilon) <= 0.0
    ):
        raise ValueError(f"harmonic_epsilon must be a finite positive number, got {epsilon!r}")

    if (
        reference_page_count is None
        or isinstance(reference_page_count, bool)
        or not isinstance(reference_page_count, (int, float))
        or not math.isfinite(float(reference_page_count))
        or float(reference_page_count) < 1.0
    ):
        raise ValueError(f"reference_page_count must be a finite number >= 1.0, got {reference_page_count!r}")


def validate_pa_score(score: Any) -> float:
    """Validate that an individual Page Authority score is a finite float in [0.0, 1.0]."""
    if score is None or isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError(f"PA score must be a float in [0.0, 1.0], got {score!r}")
    val = float(score)
    if not math.isfinite(val) or not (0.0 <= val <= 1.0):
        raise ValueError(f"PA score must be finite and in [0.0, 1.0], got {score!r}")
    return val


def canonicalize_domain_lookup(raw_domain_or_url: str) -> str:
    """
    Canonicalize a raw domain string or URL to LinkPilot's PSL site_boundary.
    Examples:
      'WWW.EXAMPLE.COM.' -> 'example.com'
      'blog.example.co.uk' -> 'example.co.uk'
      'https://shop.example.com:8080/path' -> 'example.com'
    """
    if not raw_domain_or_url or not isinstance(raw_domain_or_url, str):
        raise ValueError("Domain name must be a non-empty string.")
    cleaned = raw_domain_or_url.strip()
    if not cleaned:
        raise ValueError("Domain name must be a non-empty string.")

    if "://" in cleaned:
        norm_res = CanonicalURLNormalizer.normalize_url(cleaned)
        if norm_res.is_valid_web_url and norm_res.domain.site_boundary:
            return norm_res.domain.site_boundary
        parsed = urlsplit(cleaned)
        host = parsed.hostname or ""
    elif "/" in cleaned or ":" in cleaned or "?" in cleaned or "#" in cleaned:
        norm_res = CanonicalURLNormalizer.normalize_url(f"https://{cleaned}")
        if norm_res.is_valid_web_url and norm_res.domain.site_boundary:
            return norm_res.domain.site_boundary
        parsed = urlsplit(f"https://{cleaned}")
        host = parsed.hostname or ""
    else:
        host = cleaned

    host = host.strip().rstrip(".")
    if not host or "." not in host and host != "localhost":
        raise ValueError(f"Invalid domain identifier: {raw_domain_or_url!r}")

    ident = classify_domain_5tuple(host)
    if not ident.site_boundary:
        raise ValueError(f"Could not resolve site boundary for domain: {raw_domain_or_url!r}")
    return ident.site_boundary


# ==============================================================================
# MATHEMATICAL COMPONENT FUNCTIONS
# ==============================================================================

def compute_regularized_harmonic_top_k(
    scores: Sequence[float],
    top_k: int = DEFAULT_DA_TOP_K,
    epsilon: float = DEFAULT_DA_EPSILON,
) -> float:
    """
    Compute the regularized harmonic mean T_k(D) over the top m = min(k, N) PA scores:
      T_k(D) = 0.0 if N == 0 or sum(top_m) == 0.0
      T_k(D) = max(0.0, (m / sum(1.0 / (s_i + epsilon))) - epsilon)
    """
    if top_k is None or isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError(f"top_k must be an integer >= 1, got {top_k!r}")
    if (
        epsilon is None
        or isinstance(epsilon, bool)
        or not isinstance(epsilon, (int, float))
        or not math.isfinite(float(epsilon))
        or float(epsilon) <= 0.0
    ):
        raise ValueError(f"epsilon must be a finite positive float, got {epsilon!r}")

    validated = [validate_pa_score(s) for s in scores]
    if not validated:
        return 0.0

    sorted_desc = sorted(validated, reverse=True)
    m = min(top_k, len(sorted_desc))
    top_m = sorted_desc[:m]

    if sum(top_m) == 0.0:
        return 0.0

    eps = float(epsilon)
    denom = sum(1.0 / (s + eps) for s in top_m)
    val = (float(m) / denom) - eps
    return min(1.0, max(0.0, val))


def compute_domain_breadth_score(
    scored_pages_count: int,
    reference_page_count: float = DEFAULT_DA_REF_N,
) -> float:
    """
    Compute logarithmic domain breadth score B(D):
      B(D) = min(1.0, log10(1 + N) / log10(1 + N_ref)) for N > 0, else 0.0.
    """
    if (
        reference_page_count is None
        or isinstance(reference_page_count, bool)
        or not isinstance(reference_page_count, (int, float))
        or not math.isfinite(float(reference_page_count))
        or float(reference_page_count) < 1.0
    ):
        raise ValueError(f"reference_page_count must be >= 1.0, got {reference_page_count!r}")

    if scored_pages_count is None or isinstance(scored_pages_count, bool) or not isinstance(scored_pages_count, int):
        raise ValueError(f"scored_pages_count must be an integer, got {scored_pages_count!r}")

    if scored_pages_count <= 0:
        return 0.0

    num = math.log10(1.0 + float(scored_pages_count))
    den = math.log10(1.0 + float(reference_page_count))
    return min(1.0, max(0.0, num / den))


def compute_single_domain_authority(
    domain_name: str,
    scores: Sequence[float],
    alpha: float = DEFAULT_DA_ALPHA,
    beta: float = DEFAULT_DA_BETA,
    gamma: float = DEFAULT_DA_GAMMA,
    top_k: int = DEFAULT_DA_TOP_K,
    epsilon: float = DEFAULT_DA_EPSILON,
    reference_page_count: float = DEFAULT_DA_REF_N,
    top_page_urls: Optional[List[str]] = None,
) -> DomainAuthorityComputation:
    """
    Compute complete LP-DA metrics for a single domain from its constituent PA scores.
    """
    if not domain_name or not isinstance(domain_name, str) or not domain_name.strip():
        raise ValueError("domain_name must be a non-empty string.")

    validate_da_parameters(
        alpha=alpha,
        beta=beta,
        gamma=gamma,
        top_k=top_k,
        epsilon=epsilon,
        reference_page_count=reference_page_count,
    )

    validated = [validate_pa_score(s) for s in scores]
    n = len(validated)

    if n == 0:
        return DomainAuthorityComputation(
            domain_name=domain_name.strip(),
            lp_da_score=0.0,
            lp_da_display=0.0,
            anchor_page_pa=0.0,
            top_k_harmonic_pa=0.0,
            mean_page_pa=0.0,
            breadth_score=0.0,
            scored_pages_count=0,
            configured_top_k=top_k,
            harmonic_epsilon=float(epsilon),
            reference_page_count=float(reference_page_count),
            alpha=float(alpha),
            beta=float(beta),
            gamma=float(gamma),
            algorithm_version=DA_ALGORITHM_VERSION,
            top_page_urls=[],
        )

    anchor_pa = max(validated)
    top_k_pa = compute_regularized_harmonic_top_k(validated, top_k=top_k, epsilon=epsilon)
    breadth = compute_domain_breadth_score(n, reference_page_count=reference_page_count)
    mean_pa = sum(validated) / float(n)

    raw_da = (float(alpha) * anchor_pa) + (float(beta) * top_k_pa) + (float(gamma) * breadth * mean_pa)
    lp_da = min(1.0, max(0.0, raw_da))
    lp_da_disp = round(lp_da * 100.0, 2)

    return DomainAuthorityComputation(
        domain_name=domain_name.strip(),
        lp_da_score=lp_da,
        lp_da_display=lp_da_disp,
        anchor_page_pa=anchor_pa,
        top_k_harmonic_pa=top_k_pa,
        mean_page_pa=mean_pa,
        breadth_score=breadth,
        scored_pages_count=n,
        configured_top_k=top_k,
        harmonic_epsilon=float(epsilon),
        reference_page_count=float(reference_page_count),
        alpha=float(alpha),
        beta=float(beta),
        gamma=float(gamma),
        algorithm_version=DA_ALGORITHM_VERSION,
        top_page_urls=list(top_page_urls) if top_page_urls else [],
    )


def reconstruct_lp_da_level1(
    anchor_page_pa: float,
    top_k_harmonic_pa: float,
    breadth_score: float,
    mean_page_pa: float,
    alpha: float = DEFAULT_DA_ALPHA,
    beta: float = DEFAULT_DA_BETA,
    gamma: float = DEFAULT_DA_GAMMA,
) -> float:
    """
    Level 1 Audit Reconstruction: Reconstruct LP-DA directly from persisted aggregate components.
    """
    raw = (float(alpha) * float(anchor_page_pa)) + (float(beta) * float(top_k_harmonic_pa)) + (
        float(gamma) * float(breadth_score) * float(mean_page_pa)
    )
    return min(1.0, max(0.0, raw))


def aggregate_domain_authority_from_page_records(
    page_records: Sequence[Dict[str, Any]],
    alpha: float = DEFAULT_DA_ALPHA,
    beta: float = DEFAULT_DA_BETA,
    gamma: float = DEFAULT_DA_GAMMA,
    top_k: int = DEFAULT_DA_TOP_K,
    epsilon: float = DEFAULT_DA_EPSILON,
    reference_page_count: float = DEFAULT_DA_REF_N,
    score_internal_only: bool = True,
    computed_at: Optional[datetime] = None,
) -> List[DomainAuthorityComputation]:
    """
    Group in-memory Page Authority records by domain, apply deterministic tie-breaking:
      (-lp_pa_score, -raw_pagerank, normalized_url)
    and compute DomainAuthorityComputation objects sorted by domain_name ASC.
    """
    validate_da_parameters(
        alpha=alpha,
        beta=beta,
        gamma=gamma,
        top_k=top_k,
        epsilon=epsilon,
        reference_page_count=reference_page_count,
    )

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for rec in page_records:
        if score_internal_only and not rec.get("is_internal_page", True):
            continue
        dom = rec.get("domain")
        if not dom or not isinstance(dom, str) or not dom.strip():
            raise ValueError(f"Page record missing valid domain: {rec!r}")
        dom_key = dom.strip()
        grouped.setdefault(dom_key, []).append(rec)

    results: List[DomainAuthorityComputation] = []
    for dom_name in sorted(grouped.keys()):
        recs = grouped[dom_name]
        # Deterministic ordering: 1) lp_pa_score DESC, 2) raw_pagerank DESC, 3) normalized_url ASC
        sorted_recs = sorted(
            recs,
            key=lambda r: (
                -validate_pa_score(r.get("lp_pa_score")),
                -float(r.get("raw_pagerank", 0.0)),
                str(r.get("normalized_url", "")),
            ),
        )
        scores = [validate_pa_score(r["lp_pa_score"]) for r in sorted_recs]
        top_urls = [str(r.get("normalized_url", "")) for r in sorted_recs[:min(top_k, len(sorted_recs))]]

        comp = compute_single_domain_authority(
            domain_name=dom_name,
            scores=scores,
            alpha=alpha,
            beta=beta,
            gamma=gamma,
            top_k=top_k,
            epsilon=epsilon,
            reference_page_count=reference_page_count,
            top_page_urls=top_urls,
        )
        results.append(comp)

    return results
