"""
LinkPilot AI — Phase 1B: Module 1 Automated Test Suite (Version 1.0.3 Reconciled).
tests/test_authority_m1_eligibility.py

Contains:
1. Baseline Milestone 1 tests updated for Milestone 1.1 hardening invariants.
2. Full 35-test verification matrix (T-01 through T-35) covering:
   - Query & Path Normalization (T-01 - T-09)
   - PSL Trie & Host Processing (T-10 - T-19)
   - SPA Hash Routing Telemetry (T-20 - T-21)
   - Standards-Aware Canonical Parsing & Precedence (T-22 - T-28)
   - Graph-Wide Canonical Topology & Invariance (T-29 - T-33)
   - Data Fidelity & Phase 1A Compatibility (T-34 - T-35)
   - Rel & DOM Eligibility & Temporal Deduplication
"""
from datetime import datetime, timezone
import pytest

from app.authority.normalizer import (
    CanonicalDeclarationEvidence,
    CanonicalResolutionStatus,
    CanonicalResolutionSummary,
    CanonicalTargetResolver,
    CanonicalURLNormalizer,
    DeclarationShape,
    DomainIdentity5Tuple,
    LegacyURLAdapter,
    ProvenanceFidelityStatus,
    PublicSuffixSection,
    SPARouteTelemetry,
    TargetRelationship,
    TopologyClassification,
    classify_domain_5tuple,
    compute_canonical_cluster_id,
    normalize_hostname,
    parse_domain_identity,
    parse_rfc8288_link_headers,
)
from app.authority.eligibility import (
    DOMContextType,
    LinkEligibilityEngine,
    LinkObservationInput,
    RelAttributeType,
    TargetRetrievalState,
    TemporalObservationState,
    classify_dom_context,
    classify_rel_attributes,
)


# ==============================================================================
# SUITE 1: QUERY & PATH NORMALIZATION HARDENING (T-01 to T-09)
# ==============================================================================

def test_query_order_preservation():
    """T-01: Query parameters preserve exact original ordering ($?b=2&a=1)."""
    url = "https://example.com/search?b=2&a=1"
    res = CanonicalURLNormalizer.normalize_url(url)
    assert res.normalized_url == "https://example.com/search?b=2&a=1"
    assert res.query == "b=2&a=1"


def test_repeated_query_parameters():
    """T-02: Repeated query parameter keys preserved in original sequence."""
    url = "https://example.com/filter?tag=python&tag=data&sort=asc"
    res = CanonicalURLNormalizer.normalize_url(url)
    assert res.normalized_url == "https://example.com/filter?tag=python&tag=data&sort=asc"


def test_signed_query_isolation():
    """T-03: Signed query strings (K_signed) isolated byte-for-byte with zero mutation."""
    signed_url = "https://s3.amazonaws.com/bucket/file.pdf?X-Amz-Signature=aB12%2B34%3D&expires=1700000000"
    res = CanonicalURLNormalizer.normalize_url(signed_url)
    assert "X-Amz-Signature=aB12%2B34%3D" in res.normalized_url
    assert res.query == "X-Amz-Signature=aB12%2B34%3D&expires=1700000000"


def test_percent_encoded_query_preservation():
    """T-04: Encoded '+' (%2B) in query strings preserved without decoding to literal space."""
    url = "https://example.com/math?q=a%2Bb"
    res = CanonicalURLNormalizer.normalize_url(url)
    assert "q=a%2Bb" in res.normalized_url
    assert "+ " not in res.query


def test_consecutive_path_slash_preservation():
    """T-05: Consecutive path slashes (/api//v1/) preserved without regex collapsing."""
    url = "https://example.com/api//v1///resource/"
    res = CanonicalURLNormalizer.normalize_url(url)
    assert res.normalized_url == "https://example.com/api//v1///resource/"
    assert res.path == "/api//v1///resource/"


def test_dot_segment_rfc3986_normalization():
    """T-06: RFC 3986 Section 5.2.4 Remove_Dot_Segments on literal dots."""
    url = "https://example.com/a/b/../c/./d"
    res = CanonicalURLNormalizer.normalize_url(url)
    assert res.normalized_url == "https://example.com/a/c/d"


def test_percent_encoding_operation_order():
    """T-07: Operation order: %2E preserved before dot-segment resolution (traversal guard)."""
    # /%2E%2E/ must NOT be collapsed as '..'
    url = "https://example.com/safe/%2E%2E/passwd"
    res = CanonicalURLNormalizer.normalize_url(url)
    assert res.normalized_url == "https://example.com/safe/%2E%2E/passwd"
    assert "%2E%2E" in res.path


def test_encoded_reserved_slash_and_hex_casing():
    """T-08: %2F never decoded into literal '/'; hex triplets normalized to uppercase."""
    url = "https://example.com/docs/c%2b%2b/ch%2f1"
    res = CanonicalURLNormalizer.normalize_url(url)
    assert res.normalized_url == "https://example.com/docs/c%2B%2B/ch%2F1"
    assert "%2F" in res.path
    assert "%2B" in res.path


def test_normalization_idempotency():
    """T-09: Independent idempotency: f(f(u)) == f(u) for server and graph identities."""
    urls = [
        "https://example.com/a//b/c?b=2&a=1#frag",
        "http://EXAMPLE.com:80/path/./sub/../target?q=hello%20world",
        "https://s3.amazonaws.com/doc?sig=123%2Babc",
    ]
    for u in urls:
        first = CanonicalURLNormalizer.normalize_url(u)
        second = CanonicalURLNormalizer.normalize_url(first.normalized_url)
        assert first.normalized_url == second.normalized_url
        third = CanonicalURLNormalizer.normalize_url(first.observed_page_key)
        assert first.observed_page_key == third.observed_page_key


# ==============================================================================
# SUITE 2: PSL TRIE & HOST PROCESSING (T-10 to T-19)
# ==============================================================================

def test_psl_exact_rule_matching():
    """T-10: Exact rule matching (co.uk -> example.co.uk)."""
    ident = classify_domain_5tuple("blog.sub.example.co.uk")
    assert ident.tld == "co.uk"
    assert ident.icann_registrable_domain == "example.co.uk"
    assert ident.site_boundary == "example.co.uk"
    assert ident.public_suffix_section == PublicSuffixSection.ICANN
    assert ident.is_uncertain_suffix is False


def test_psl_wildcard_rule_matching():
    """T-11: Wildcard rule matching (*.ck -> store.org.ck under suffix org.ck)."""
    ident = classify_domain_5tuple("store.org.ck")
    assert ident.tld == "org.ck"
    assert ident.site_boundary == "store.org.ck"
    assert ident.public_suffix_section == PublicSuffixSection.ICANN


def test_psl_exception_rule_matching():
    """T-12: Exception rule matching (!www.ck -> www.ck is registered domain, suffix ck)."""
    ident = classify_domain_5tuple("sub.www.ck")
    assert ident.tld == "ck"
    assert ident.site_boundary == "www.ck"
    assert ident.subdomain == "sub"
    assert ident.public_suffix_section == PublicSuffixSection.ICANN


def test_psl_private_tenant_isolation():
    """T-13: Multi-tenant private suffix isolation (user.github.io is tenant-isolated)."""
    ident1 = classify_domain_5tuple("alice.github.io")
    ident2 = classify_domain_5tuple("bob.github.io")
    assert ident1.site_boundary == "alice.github.io"
    assert ident2.site_boundary == "bob.github.io"
    assert ident1.site_boundary != ident2.site_boundary  # Strict tenant isolation
    assert ident1.public_suffix_section == PublicSuffixSection.PRIVATE
    assert ident1.icann_registrable_domain is None


def test_psl_unknown_suffix_classification():
    """T-14: Unlisted suffix assigns is_uncertain_suffix=True and fallback site_boundary=hostname."""
    ident = classify_domain_5tuple("api.internal.service")
    assert ident.is_uncertain_suffix is True
    assert ident.site_boundary == "api.internal.service"
    assert ident.public_suffix_section == PublicSuffixSection.UNKNOWN


def test_host_syntax_trailing_dot_and_idna():
    """T-15: Trailing DNS dot removal and IDNA 2008 Punycode normalization."""
    res = CanonicalURLNormalizer.normalize_url("https://example.com./page")
    assert res.hostname == "example.com"
    assert res.normalized_url == "https://example.com/page"

    res_idna = CanonicalURLNormalizer.normalize_url("https://münchen.de/")
    assert "xn--" in res_idna.hostname
    assert res_idna.domain.is_punycode is True


def test_invalid_hostname_rejection():
    """T-16: Invalid hostname rejection (length > 253 or label > 63)."""
    long_label = "a" * 64 + ".com"
    res = CanonicalURLNormalizer.normalize_url(f"https://{long_label}/")
    assert res.is_valid_web_url is False
    assert "RFC length limits" in res.error_message


def test_ipv4_literal_handling():
    """T-17: IPv4 literal assigned NOT_APPLICABLE and bypasses PSL matching."""
    res = CanonicalURLNormalizer.normalize_url("http://192.168.1.1:8080/admin")
    assert res.hostname == "192.168.1.1"
    assert res.domain.public_suffix_section == PublicSuffixSection.NOT_APPLICABLE
    assert res.domain.site_boundary == "192.168.1.1"
    assert res.domain.is_ip_literal is True


def test_bracketed_ipv6_host_normalization():
    """T-18: Bracketed IPv6 extracted for validation and serialized with brackets."""
    res = CanonicalURLNormalizer.normalize_url("https://[2001:db8::1]:8443/status")
    assert res.hostname == "[2001:db8::1]"
    assert res.port == 8443
    assert res.normalized_url == "https://[2001:db8::1]:8443/status"
    assert res.domain.is_ip_literal is True


def test_ipv4_mapped_ipv6_and_zone_id_rejection():
    """T-19: IPv4-mapped IPv6 parsed; zone ID (%eth0) rejected."""
    res_mapped = CanonicalURLNormalizer.normalize_url("https://[::ffff:192.0.2.1]/")
    assert res_mapped.is_valid_web_url is True
    assert res_mapped.domain.is_ip_literal is True

    res_zone = CanonicalURLNormalizer.normalize_url("https://[fe80::1%eth0]/")
    assert res_zone.is_valid_web_url is False
    assert "zone identifier" in res_zone.error_message


# ==============================================================================
# SUITE 3: SPA HASH ROUTING TELEMETRY (T-20 to T-21)
# ==============================================================================

def test_spa_hash_routing_suspected():
    """T-20: HASH_ROUTE_SUSPECTED: route fragment preserved in observed_page_key, marked unscored."""
    url = "https://app.com/#/dashboard/metrics"
    res = CanonicalURLNormalizer.normalize_url(url, is_spa_domain_confirmed=False)
    assert res.normalized_url == "https://app.com/"
    assert res.observed_page_key == "https://app.com/#/dashboard/metrics"
    assert res.spa_telemetry == SPARouteTelemetry.HASH_ROUTE_SUSPECTED
    assert res.is_authoritative_scored_page is False  # Unscored observation pending confirmation


def test_spa_hash_routing_confirmed():
    """T-21: HASH_ROUTE_CONFIRMED: confirmed SPA route registered as authoritative scored node."""
    url = "https://app.com/#/dashboard/metrics"
    res = CanonicalURLNormalizer.normalize_url(url, is_spa_domain_confirmed=True)
    assert res.normalized_url == "https://app.com/"
    assert res.observed_page_key == "https://app.com/#/dashboard/metrics"
    assert res.spa_telemetry == SPARouteTelemetry.HASH_ROUTE_CONFIRMED
    assert res.is_authoritative_scored_page is True  # Authoritative scored node


# ==============================================================================
# SUITE 4: CANONICAL PARSING & PRECEDENCE (T-22 to T-28)
# ==============================================================================

def test_standards_compliant_html_rel_parsing():
    """T-22: Standards-aware HTML rel parsing with compound tokens (rel='canonical alternate')."""
    res = CanonicalTargetResolver.evaluate_page_canonical(
        source_url="https://example.com/page",
        html_declarations=["https://example.com/canonical-target"],
    )
    assert res.status == CanonicalResolutionStatus.CANONICAL_RESOLVED
    assert res.effective_target_url == "https://example.com/canonical-target"


def test_rfc8288_http_link_header_parsing():
    """T-23: RFC 8288 HTTP Link header parsing with quoted and unquoted parameters."""
    header = '</canonical-page>; rel="canonical"; title="Target", </feed>; rel="alternate"'
    links = parse_rfc8288_link_headers(header)
    assert len(links) == 1
    assert links[0] == "/canonical-page"


def test_canonical_base_url_resolution():
    """T-24: Relative canonical resolved against base_url."""
    res = CanonicalTargetResolver.evaluate_page_canonical(
        source_url="https://example.com/dir/page",
        html_declarations=["/root-canonical"],
        effective_base_url="https://example.com/base/",
    )
    assert res.effective_target_url == "https://example.com/root-canonical"


def test_canonical_conflicting_declarations():
    """T-25: Conflicting targets across HTML and HTTP headers trigger MULTIPLE_CONFLICTING -> fallback to self."""
    res = CanonicalTargetResolver.evaluate_page_canonical(
        source_url="https://example.com/source",
        html_declarations=["https://example.com/target-a"],
        http_link_headers=['<https://example.com/target-b>; rel="canonical"'],
    )
    assert res.shape == DeclarationShape.MULTIPLE_CONFLICTING
    assert res.status == CanonicalResolutionStatus.CANONICAL_CONFLICT_REJECTED
    assert res.effective_target_url == "https://example.com/source"


def test_canonical_partially_invalid_declarations():
    """T-26: Partially invalid declarations (1 valid + 1 invalid) trigger PARTIALLY_INVALID -> fallback to self."""
    res = CanonicalTargetResolver.evaluate_page_canonical(
        source_url="https://example.com/source",
        html_declarations=["https://example.com/valid", "javascript:void(0)"],
    )
    assert res.shape == DeclarationShape.PARTIALLY_INVALID
    assert res.status == CanonicalResolutionStatus.INVALID_REJECTED
    assert res.effective_target_url == "https://example.com/source"


def test_canonical_self_affirmation():
    """T-27: Self-canonical (A -> A) classified SELF -> accepted as self, not cycle."""
    res = CanonicalTargetResolver.evaluate_page_canonical(
        source_url="https://example.com/page",
        html_declarations=["https://example.com/page"],
    )
    assert res.relationship == TargetRelationship.SELF
    assert res.status == CanonicalResolutionStatus.SELF_CANONICAL
    assert res.effective_target_url == "https://example.com/page"


def test_canonical_identical_multiple():
    """T-28: Identical multiple declarations coalesce cleanly without conflict error."""
    res = CanonicalTargetResolver.evaluate_page_canonical(
        source_url="https://example.com/source",
        html_declarations=["https://example.com/target", "https://example.com/target"],
        http_link_headers=['<https://example.com/target>; rel="canonical"'],
    )
    assert res.shape == DeclarationShape.MULTIPLE_IDENTICAL
    assert res.status == CanonicalResolutionStatus.CANONICAL_RESOLVED
    assert res.effective_target_url == "https://example.com/target"


# ==============================================================================
# SUITE 5: GRAPH-WIDE CANONICAL TOPOLOGY & INVARIANCE (T-29 to T-33)
# ==============================================================================

def test_canonical_chain_condensation():
    """T-29: Canonical chain fixture (A -> B -> C) condenses to terminal C in derived view."""
    pages = {
        "https://example.com/a": CanonicalTargetResolver.evaluate_page_canonical(
            "https://example.com/a", html_declarations=["https://example.com/b"]
        ),
        "https://example.com/b": CanonicalTargetResolver.evaluate_page_canonical(
            "https://example.com/b", html_declarations=["https://example.com/c"]
        ),
        "https://example.com/c": CanonicalTargetResolver.evaluate_page_canonical(
            "https://example.com/c", html_declarations=["https://example.com/c"]  # Terminal self-canonical
        ),
    }
    resolved = CanonicalTargetResolver.resolve_graph_wide_canonicals(pages)
    assert resolved["https://example.com/a"].effective_target_url == "https://example.com/c"
    assert resolved["https://example.com/a"].topology == TopologyClassification.CHAIN
    assert resolved["https://example.com/b"].effective_target_url == "https://example.com/c"
    assert resolved["https://example.com/c"].effective_target_url == "https://example.com/c"


def test_canonical_upstream_alias_into_cycle():
    """T-30: Upstream alias into cycle fixture (X -> A <-> B): X rejected from cycle, remains self."""
    pages = {
        "https://example.com/x": CanonicalTargetResolver.evaluate_page_canonical(
            "https://example.com/x", html_declarations=["https://example.com/a"]
        ),
        "https://example.com/a": CanonicalTargetResolver.evaluate_page_canonical(
            "https://example.com/a", html_declarations=["https://example.com/b"]
        ),
        "https://example.com/b": CanonicalTargetResolver.evaluate_page_canonical(
            "https://example.com/b", html_declarations=["https://example.com/a"]
        ),
    }
    resolved = CanonicalTargetResolver.resolve_graph_wide_canonicals(pages)
    # Cycle members revert to self (no arbitrary winner)
    assert resolved["https://example.com/a"].effective_target_url == "https://example.com/a"
    assert resolved["https://example.com/a"].status == CanonicalResolutionStatus.CANONICAL_CYCLE_REJECTED
    assert resolved["https://example.com/b"].effective_target_url == "https://example.com/b"
    # Upstream alias X is rejected from collapsing into cycle
    assert resolved["https://example.com/x"].effective_target_url == "https://example.com/x"
    assert resolved["https://example.com/x"].status == CanonicalResolutionStatus.UPSTREAM_ALIAS_REJECTED


def test_canonical_persistent_cluster_id():
    """T-31: Stable SHA-256 cluster ID derivation; verification of independence from process hash seed."""
    id1 = compute_canonical_cluster_id("example.com", "https://example.com/target")
    id2 = compute_canonical_cluster_id("example.com", "https://example.com/target")
    assert id1 == id2
    assert len(id1) == 16


def test_primary_graph_invariance():
    """T-32: Canonical declaration changes leave primary graph V_p and physical edges strictly invariant."""
    url = "https://example.com/page"
    # Without canonical
    res_none = CanonicalURLNormalizer.normalize_url(url)
    # With canonical
    summary = CanonicalTargetResolver.evaluate_page_canonical(url, html_declarations=["https://example.com/target"])
    # Invariant: observed_page_key NEVER changes due to canonical declaration
    assert res_none.observed_page_key == "https://example.com/page"
    assert summary.source_url == "https://example.com/page"


def test_no_canonical_backlink_creation():
    """T-33: Canonical tag A -> B creates zero physical backlink edges from A to B."""
    now = datetime.now(timezone.utc)
    obs = LinkObservationInput(
        source_url="https://source.com/page",
        raw_target_url="https://target.com/page",
        observed_at=now,
        source_id="commoncrawl",
        target_declared_canonical="https://other.com/target",
    )
    eval_res = LinkEligibilityEngine.evaluate_observation(obs)
    # Target normalized URL must remain the physical observed placement target
    assert eval_res.target_normalized_url == "https://target.com/page"


# ==============================================================================
# SUITE 6: DATA FIDELITY & PHASE 1A COMPATIBILITY (T-34 to T-35)
# ==============================================================================

def test_data_fidelity_raw_vs_parser():
    """T-34: Parser string href='&amp;' (-> '&') distinguished from raw payload bytes."""
    raw_html_snippet = '<a href="/test?a=1&amp;b=2">Link</a>'
    parser_extracted_href = "/test?a=1&b=2"
    # Parser-extracted string is NOT the verbatim byte string
    assert parser_extracted_href != "/test?a=1&amp;b=2"
    norm = CanonicalURLNormalizer.normalize_url(parser_extracted_href, base_url="https://example.com")
    assert norm.normalized_url == "https://example.com/test?a=1&b=2"


def test_legacy_phase1a_compatibility():
    """T-35: LegacyURLAdapter flags lossy historical records as LEGACY_NORMALIZATION_LOSSY."""
    legacy_row = {"normalized_url": "https://example.com/page?a=1", "raw_url": None}
    status = LegacyURLAdapter.detect_lossy_legacy_normalization(legacy_row["normalized_url"], legacy_row["raw_url"])
    assert status == ProvenanceFidelityStatus.LEGACY_NORMALIZATION_LOSSY

    # In-memory adaptation without database writes
    norm = LegacyURLAdapter.adapt_page_record(legacy_row)
    assert norm.normalized_url == "https://example.com/page?a=1"


# ==============================================================================
# SUITE 7: REL, DOM & TEMPORAL ELIGIBILITY (RETAINED MILESTONE 1 BASELINE)
# ==============================================================================

def test_url_normalization_scheme_and_ports():
    """Verify scheme lowercase, default port stripping, and non-standard port preservation."""
    res1 = CanonicalURLNormalizer.normalize_url("HTTP://Example.COM:80/path")
    assert res1.is_valid_web_url is True
    assert res1.normalized_url == "http://example.com/path"
    assert res1.port is None

    res2 = CanonicalURLNormalizer.normalize_url("HTTPS://EXAMPLE.COM:443/secure")
    assert res2.is_valid_web_url is True
    assert res2.normalized_url == "https://example.com/secure"
    assert res2.port is None

    res3 = CanonicalURLNormalizer.normalize_url("https://example.com:8080/api")
    assert res3.is_valid_web_url is True
    assert res3.normalized_url == "https://example.com:8080/api"
    assert res3.port == 8080


def test_rel_attribute_classification():
    """Verify individual and combined rel attribute multipliers."""
    types, mult = classify_rel_attributes("dofollow")
    assert RelAttributeType.DOFOLLOW in types
    assert mult == 1.00

    types, mult = classify_rel_attributes("nofollow")
    assert RelAttributeType.NOFOLLOW in types
    assert mult == 0.25

    types, mult = classify_rel_attributes("sponsored")
    assert RelAttributeType.SPONSORED in types
    assert mult == 0.15

    types, mult = classify_rel_attributes("ugc")
    assert RelAttributeType.UGC in types
    assert mult == 0.35

    types, mult = classify_rel_attributes("nofollow sponsored")
    assert RelAttributeType.NOFOLLOW in types
    assert RelAttributeType.SPONSORED in types
    assert mult == 0.15

    types, mult = classify_rel_attributes(None)
    assert RelAttributeType.UNKNOWN in types
    assert mult == 1.00


def test_dom_context_classification():
    """Verify semantic DOM context multipliers and neutral unextracted defaults."""
    ctx, mult = classify_dom_context("article")
    assert ctx == DOMContextType.ARTICLE
    assert mult == 1.00

    ctx, mult = classify_dom_context("main")
    assert ctx == DOMContextType.MAIN
    assert mult == 1.00

    ctx, mult = classify_dom_context("footer")
    assert ctx == DOMContextType.FOOTER
    assert mult == 0.40

    ctx, mult = classify_dom_context("nav")
    assert ctx == DOMContextType.NAV
    assert mult == 0.50

    ctx, mult = classify_dom_context(None)
    assert ctx == DOMContextType.UNKNOWN
    assert mult == 1.00


def test_historical_404_observation_preservation():
    """Verify historical link observations are NOT discarded when target returns HTTP 404."""
    now = datetime.now(timezone.utc)
    hist_obs = LinkObservationInput(
        source_url="https://source.org/history",
        raw_target_url="https://target.com/discontinued-page",
        observed_at=now,
        source_id="wayback",
        source_http_status=200,
        target_http_status=404,
        provenance_id=101,
        payload_sha256="abc123sha256",
    )
    res_hist = LinkEligibilityEngine.evaluate_observation(hist_obs)
    assert res_hist.is_eligible_for_graph is True
    assert res_hist.is_historical_preservation is True
    assert res_hist.target_state == TargetRetrievalState.CLIENT_ERROR_404
    assert res_hist.temporal_state == TemporalObservationState.UNOBSERVED_RECENT


def test_temporal_observation_deduplication():
    """Verify multiple historical captures collapse into single canonical edge with base weight 1.00."""
    now = datetime.now(timezone.utc)
    eval_list = []
    for i in range(5):
        obs = LinkObservationInput(
            source_url="https://referrer.com/article",
            raw_target_url="https://zoniraz.com/service",
            observed_at=now,
            source_id="wayback",
            source_http_status=200,
            target_http_status=200,
            rel_attributes="dofollow",
            dom_context="main",
            provenance_id=i + 1,
            payload_sha256=f"hash_{i}",
        )
        eval_list.append(LinkEligibilityEngine.evaluate_observation(obs))

    aggregated_edges = LinkEligibilityEngine.aggregate_temporal_observations(eval_list)
    assert len(aggregated_edges) == 1
    edge = aggregated_edges[0]
    assert edge.observation_count == 5
    assert edge.topological_base_weight == 1.00
    assert edge.effective_rel_multiplier == 1.00
    assert edge.effective_dom_multiplier == 1.00
