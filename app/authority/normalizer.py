"""
LinkPilot AI — Phase 1B: Module 1 Hardened Normalization Layer.
app/authority/normalizer.py

Implements:
1. Five URL Identity Entities & Primary Graph Key Rules.
2. Strict 8-step percent-encoding operation order with signed query isolation.
3. Offline Mozilla Public Suffix List (PSL) Trie with ICANN & PRIVATE sections,
   wildcard (*.ck), exception (!www.ck), and IPv4/IPv6 literal handling.
4. Three-Dimensional Canonical Classification (Shape, Target Relationship, Topology)
   with Standards-Aware HTML rel & RFC 8288 HTTP Link Header parsing.
5. Graph-Wide Canonical Topology Resolution with Tarjan SCC cycle breaking,
   chain condensation, and stable SHA-256 cluster ID derivation.
6. SPA hash routing classification and LegacyURLAdapter.
"""
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import ipaddress
import os
import posixpath
import re
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit


# ==============================================================================
# ENUMS & DATA STRUCTURES
# ==============================================================================

class PublicSuffixSection(str, Enum):
    ICANN = "ICANN"
    PRIVATE = "PRIVATE"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class SPARouteTelemetry(str, Enum):
    STANDARD_FRAGMENT = "STANDARD_FRAGMENT"
    HASH_ROUTE_SUSPECTED = "HASH_ROUTE_SUSPECTED"
    HASH_ROUTE_CONFIRMED = "HASH_ROUTE_CONFIRMED"
    FRAGMENT_ROUTING_UNKNOWN = "FRAGMENT_ROUTING_UNKNOWN"


class DeclarationShape(str, Enum):
    NONE = "NONE"
    SINGLE = "SINGLE"
    MULTIPLE_IDENTICAL = "MULTIPLE_IDENTICAL"
    MULTIPLE_CONFLICTING = "MULTIPLE_CONFLICTING"
    INVALID = "INVALID"
    PARTIALLY_INVALID = "PARTIALLY_INVALID"


class TargetRelationship(str, Enum):
    SELF = "SELF"
    SAME_SITE_BOUNDARY = "SAME_SITE_BOUNDARY"
    CROSS_SITE_BOUNDARY = "CROSS_SITE_BOUNDARY"
    UNOBSERVED = "UNOBSERVED"


class TopologyClassification(str, Enum):
    DIRECT = "DIRECT"
    CHAIN = "CHAIN"
    CYCLE = "CYCLE"
    UPSTREAM_ALIAS_TO_CYCLE = "UPSTREAM_ALIAS_TO_CYCLE"


class CanonicalResolutionStatus(str, Enum):
    SELF_CANONICAL = "SELF_CANONICAL"
    CANONICAL_RESOLVED = "CANONICAL_RESOLVED"
    CANONICAL_CONFLICT_REJECTED = "CANONICAL_CONFLICT_REJECTED"
    INVALID_REJECTED = "INVALID_REJECTED"
    CANONICAL_CYCLE_REJECTED = "CANONICAL_CYCLE_REJECTED"
    UPSTREAM_ALIAS_REJECTED = "UPSTREAM_ALIAS_REJECTED"
    CROSS_DOMAIN_FIREWALLED = "CROSS_DOMAIN_FIREWALLED"
    NO_CANONICAL = "NO_CANONICAL"
    CANONICAL_CHAIN_FLATTENED = "CANONICAL_CHAIN_FLATTENED"


class ProvenanceFidelityStatus(str, Enum):
    EXACT_RAW_OBSERVATION = "EXACT_RAW_OBSERVATION"
    PARSER_DERIVED = "PARSER_DERIVED"
    LEGACY_NORMALIZATION_LOSSY = "LEGACY_NORMALIZATION_LOSSY"
    SYNTHESIZED_HISTORICAL = "SYNTHESIZED_HISTORICAL"


@dataclass(frozen=True)
class DomainIdentity5Tuple:
    """Five-tuple domain classification according to LinkPilot Phase 1B."""
    hostname: str
    icann_registrable_domain: Optional[str]
    psl_registrable_domain: Optional[str]
    site_boundary: str
    public_suffix_section: PublicSuffixSection
    is_uncertain_suffix: bool
    is_ip_literal: bool = False
    is_punycode: bool = False
    subdomain: Optional[str] = None
    tld: str = ""

    # Backward compatibility properties with Phase 1A / Milestone 1
    @property
    def registered_domain(self) -> str:
        return self.site_boundary

    @property
    def is_subdomain(self) -> bool:
        return self.subdomain is not None and len(self.subdomain) > 0


@dataclass(frozen=True)
class NormalizedURLResult:
    """Deterministic result of URL normalization across representation tiers."""
    raw_url: str
    normalized_url: str                # Tier 2: fragment-free server resource
    observed_page_key: str             # Tier 3: primary graph vertex
    scheme: str
    hostname: str
    port: Optional[int]
    path: str
    query: str
    fragment: str
    domain: DomainIdentity5Tuple
    is_valid_web_url: bool
    spa_telemetry: SPARouteTelemetry = SPARouteTelemetry.STANDARD_FRAGMENT
    is_authoritative_scored_page: bool = True
    error_message: Optional[str] = None

    # Backward-compatible tuple access
    def __iter__(self):
        yield self.raw_url
        yield self.normalized_url
        yield self.scheme
        yield self.hostname
        yield self.port
        yield self.path
        yield self.query
        yield self.domain
        yield self.is_valid_web_url
        yield self.error_message


# Sensitive query parameter keys that require byte-for-byte isolation
SIGNED_QUERY_KEYS = frozenset({
    "signature", "sig", "x-amz-signature", "token", "auth", "hmac", "expires", "key"
})

# RFC 3986 Unreserved Characters: ALPHA, DIGIT, '-', '.', '_', '~'
# Note: '.' (%2E) is explicitly excluded from decoding during step 3.
UNRESERVED_SAFE_CHARS = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_~"
)


# ==============================================================================
# OFFLINE PUBLIC SUFFIX LIST (PSL) TRIE ENGINE
# ==============================================================================

class PSLNode:
    def __init__(self):
        self.children: Dict[str, "PSLNode"] = {}
        self.is_rule: bool = False
        self.section: PublicSuffixSection = PublicSuffixSection.UNKNOWN
        self.is_wildcard: bool = False
        self.is_exception: bool = False


class PublicSuffixListTrie:
    """
    Offline Longest-Match Trie for Mozilla Public Suffix List.
    Parses both ICANN and PRIVATE sections.
    Supports exact rules, wildcard rules (*.ck), and exception rules (!www.ck).
    """
    _instance: Optional["PublicSuffixListTrie"] = None

    def __init__(self, dat_path: Optional[str] = None):
        self.root = PSLNode()
        self.rules_loaded = 0
        if dat_path is None:
            dat_path = os.path.join(os.path.dirname(__file__), "resources", "public_suffix_list.dat")
        self._load_psl(dat_path)

    @classmethod
    def get_instance(cls) -> "PublicSuffixListTrie":
        if cls._instance is None:
            cls._instance = PublicSuffixListTrie()
        return cls._instance

    def _load_psl(self, dat_path: str):
        if not os.path.exists(dat_path):
            return

        current_section = PublicSuffixSection.UNKNOWN
        with open(dat_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("// ===BEGIN ICANN DOMAINS==="):
                    current_section = PublicSuffixSection.ICANN
                    continue
                elif line.startswith("// ===END ICANN DOMAINS==="):
                    current_section = PublicSuffixSection.UNKNOWN
                    continue
                elif line.startswith("// ===BEGIN PRIVATE DOMAINS==="):
                    current_section = PublicSuffixSection.PRIVATE
                    continue
                elif line.startswith("// ===END PRIVATE DOMAINS==="):
                    current_section = PublicSuffixSection.UNKNOWN
                    continue
                elif line.startswith("//"):
                    continue

                if current_section != PublicSuffixSection.UNKNOWN:
                    self._add_rule(line, current_section)

    def _add_rule(self, rule_text: str, section: PublicSuffixSection):
        rule = rule_text.lower().strip()
        is_exception = rule.startswith("!")
        if is_exception:
            rule = rule[1:]

        is_wildcard = rule.startswith("*.")
        if is_wildcard:
            rule = rule[2:]

        # Split rule into labels from right to left (tld first)
        labels = [l for l in rule.split(".") if l]
        labels.reverse()

        curr = self.root
        for label in labels:
            if label not in curr.children:
                curr.children[label] = PSLNode()
            curr = curr.children[label]

        curr.is_rule = True
        curr.section = section
        curr.is_wildcard = is_wildcard
        curr.is_exception = is_exception
        self.rules_loaded += 1

    def find_public_suffix(self, hostname: str) -> Tuple[Optional[str], PublicSuffixSection, bool]:
        """
        Find matching public suffix for a hostname.
        Returns: (suffix_str, section, is_exception)
        """
        labels = [l for l in hostname.lower().strip().split(".") if l]
        if not labels:
            return None, PublicSuffixSection.UNKNOWN, False

        rev_labels = list(reversed(labels))
        matching_rules: List[Tuple[List[str], PublicSuffixSection, bool, bool]] = []
        # Each item: (rule_labels_rev, section, is_wildcard, is_exception)

        def _traverse(node: PSLNode, idx: int, path: List[str]):
            if node.is_rule:
                matching_rules.append((list(path), node.section, node.is_wildcard, node.is_exception))

            if idx >= len(rev_labels):
                return

            label = rev_labels[idx]
            if label in node.children:
                _traverse(node.children[label], idx + 1, path + [label])

        _traverse(self.root, 0, [])

        if not matching_rules:
            return None, PublicSuffixSection.UNKNOWN, False

        # Longest-match rule evaluation
        # Exception rules take absolute precedence
        for path, section, is_wildcard, is_exception in matching_rules:
            if is_exception:
                # An exception rule defines that the matched path minus 1 label is the suffix
                # e.g., !www.ck -> suffix is 'ck', www.ck is registered domain
                suffix_labels = list(reversed(path[:-1]))
                return ".".join(suffix_labels), section, True

        # Sort matching rules by specificity (length of matched path)
        matching_rules.sort(key=lambda item: len(item[0]), reverse=True)
        best_path, best_section, best_wildcard, _ = matching_rules[0]

        if best_wildcard:
            # Under a wildcard rule (*.ck), the child label is also part of the public suffix
            matched_len = len(best_path)
            if matched_len < len(rev_labels):
                wildcard_suffix_labels = list(reversed(rev_labels[:matched_len + 1]))
                return ".".join(wildcard_suffix_labels), best_section, False
            else:
                return ".".join(reversed(best_path)), best_section, False
        else:
            return ".".join(reversed(best_path)), best_section, False


def classify_domain_5tuple(hostname: str) -> DomainIdentity5Tuple:
    """
    Classify a hostname into LinkPilot's 5-Tuple Domain Identity:
    1. icann_registrable_domain
    2. psl_registrable_domain
    3. site_boundary
    4. public_suffix_section
    5. is_uncertain_suffix
    """
    if not hostname:
        return DomainIdentity5Tuple(
            hostname="",
            icann_registrable_domain=None,
            psl_registrable_domain=None,
            site_boundary="",
            public_suffix_section=PublicSuffixSection.UNKNOWN,
            is_uncertain_suffix=True,
        )

    clean_host = hostname.strip().lower().rstrip(".")

    # 1. Check IP address literal
    is_ip = False
    try:
        # Check IPv4
        ipaddress.IPv4Address(clean_host)
        is_ip = True
    except ValueError:
        pass

    if not is_ip:
        # Check IPv6 literal (strip brackets if present)
        unbracketed = clean_host[1:-1] if (clean_host.startswith("[") and clean_host.endswith("]")) else clean_host
        try:
            ipaddress.IPv6Address(unbracketed)
            is_ip = True
            clean_host = f"[{unbracketed}]"
        except ValueError:
            pass

    if is_ip:
        return DomainIdentity5Tuple(
            hostname=clean_host,
            icann_registrable_domain=None,
            psl_registrable_domain=None,
            site_boundary=clean_host,
            public_suffix_section=PublicSuffixSection.NOT_APPLICABLE,
            is_uncertain_suffix=False,
            is_ip_literal=True,
            tld="",
        )

    # 2. Punycode IDNA normalization
    is_punycode = "xn--" in clean_host
    try:
        encoded_host = clean_host.encode("idna").decode("ascii")
        if "xn--" in encoded_host:
            is_punycode = True
        clean_host = encoded_host
    except Exception:
        pass

    # 3. Single-label internal hosts
    parts = clean_host.split(".")
    if len(parts) == 1:
        return DomainIdentity5Tuple(
            hostname=clean_host,
            icann_registrable_domain=None,
            psl_registrable_domain=None,
            site_boundary=clean_host,
            public_suffix_section=PublicSuffixSection.UNKNOWN,
            is_uncertain_suffix=True,
            is_punycode=is_punycode,
            tld="",
        )

    # 4. Search PSL Trie
    psl = PublicSuffixListTrie.get_instance()
    suffix, section, is_exception = psl.find_public_suffix(clean_host)

    if not suffix:
        # Conservative fallback: treat as uncertain suffix; site_boundary = clean_host
        return DomainIdentity5Tuple(
            hostname=clean_host,
            icann_registrable_domain=None,
            psl_registrable_domain=None,
            site_boundary=clean_host,
            public_suffix_section=PublicSuffixSection.UNKNOWN,
            is_uncertain_suffix=True,
            is_punycode=is_punycode,
            tld=parts[-1],
        )

    suffix_parts = suffix.split(".")
    suffix_len = len(suffix_parts)

    if len(parts) <= suffix_len:
        # Hostname is itself a public suffix or identical
        return DomainIdentity5Tuple(
            hostname=clean_host,
            icann_registrable_domain=None,
            psl_registrable_domain=None,
            site_boundary=clean_host,
            public_suffix_section=section,
            is_uncertain_suffix=False,
            is_punycode=is_punycode,
            tld=suffix,
        )

    # Registrable domain is suffix plus one preceding label
    reg_domain = ".".join(parts[-(suffix_len + 1):])
    subdomain_parts = parts[:-(suffix_len + 1)]
    subdomain = ".".join(subdomain_parts) if subdomain_parts else None

    icann_domain = reg_domain if section == PublicSuffixSection.ICANN else None
    psl_domain = reg_domain

    # If it is in the PRIVATE section, site_boundary isolates the tenant
    site_boundary = reg_domain

    return DomainIdentity5Tuple(
        hostname=clean_host,
        icann_registrable_domain=icann_domain,
        psl_registrable_domain=psl_domain,
        site_boundary=site_boundary,
        public_suffix_section=section,
        is_uncertain_suffix=False,
        is_punycode=is_punycode,
        subdomain=subdomain,
        tld=suffix,
    )


# Backward compatibility with Phase 1A normalizer function name
def parse_domain_identity(hostname: str) -> DomainIdentity5Tuple:
    return classify_domain_5tuple(hostname)


def normalize_hostname(hostname: str) -> Tuple[str, bool]:
    ident = classify_domain_5tuple(hostname)
    return ident.hostname, ident.is_punycode


# ==============================================================================
# PERCENT-ENCODING & STRICT EIGHT-STEP URL NORMALIZATION
# ==============================================================================

def validate_percent_encoding_syntax(s: str) -> bool:
    """Validate that every '%' is followed by two hexadecimal characters."""
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "%":
            if i + 2 >= n or not all(c in "0123456789abcdefABCDEF" for c in s[i+1:i+3]):
                return False
            i += 3
        else:
            i += 1
    return True


def normalize_percent_escapes(component: str, is_query: bool = False, is_signed: bool = False) -> str:
    """
    Applies Steps 3, 4, 5, 6 of percent-escape normalization:
    - Decodes RFC 3986 unreserved characters (ALPHA, DIGIT, '-', '_', '~').
    - Preserves '%2E' / '%2e' (encoded dot) verbatim as '%2E' (NEVER decoded).
    - Preserves all reserved characters (e.g. '%2F', '%2B', '%3F').
    - Normalizes remaining hex triplets to UPPERCASE.
    - If is_signed is True, preserves query string verbatim.
    """
    if is_signed and is_query:
        return component

    if not component or "%" not in component:
        return component

    result: List[str] = []
    i = 0
    n = len(component)
    while i < n:
        if component[i] == "%" and i + 2 < n:
            hex_digits = component[i+1:i+3]
            try:
                byte_val = int(hex_digits, 16)
            except ValueError:
                result.append(component[i])
                i += 1
                continue

            # Encoded dot guard: '%2E' / '%2e' (byte 0x2E) is NEVER decoded
            if byte_val == 0x2E:
                result.append("%2E")
                i += 3
                continue

            # Unreserved character check
            if byte_val in UNRESERVED_SAFE_CHARS:
                result.append(chr(byte_val))
                i += 3
                continue

            # Reserved or non-ASCII characters: preserve with UPPERCASE hex
            result.append(f"%{hex_digits.upper()}")
            i += 3
        else:
            result.append(component[i])
            i += 1

    return "".join(result)


def rfc3986_remove_dot_segments(path: str) -> str:
    """
    Implements RFC 3986 Section 5.2.4 Remove_Dot_Segments algorithm strictly
    on literal path segments.
    """
    if not path:
        return "/"

    input_buf = path
    output_buf: List[str] = []

    while input_buf:
        # A. If the input buffer begins with a prefix of "../" or "./",
        # then remove that prefix from the input buffer.
        if input_buf.startswith("../"):
            input_buf = input_buf[3:]
        elif input_buf.startswith("./"):
            input_buf = input_buf[2:]
        # B. If the input buffer begins with a prefix of "/./" or "/.", where "." is a complete path segment,
        # then replace that prefix with "/" in the input buffer.
        elif input_buf.startswith("/./"):
            input_buf = "/" + input_buf[3:]
        elif input_buf == "/.":
            input_buf = "/"
        # C. If the input buffer begins with a prefix of "/../" or "/..", where ".." is a complete path segment,
        # then replace that prefix with "/" in the input buffer and remove the last segment from output buffer.
        elif input_buf.startswith("/../"):
            input_buf = "/" + input_buf[4:]
            if output_buf:
                output_buf.pop()
        elif input_buf == "/..":
            input_buf = "/"
            if output_buf:
                output_buf.pop()
        # D. If the input buffer consists only of "." or "..", then remove that from the input buffer.
        elif input_buf in (".", ".."):
            input_buf = ""
        # E. Move the first path segment in the input buffer to the end of the output buffer.
        else:
            if input_buf.startswith("/"):
                # Segment starts with slash
                next_slash = input_buf.find("/", 1)
                if next_slash != -1:
                    segment = input_buf[:next_slash]
                    input_buf = input_buf[next_slash:]
                else:
                    segment = input_buf
                    input_buf = ""
            else:
                next_slash = input_buf.find("/")
                if next_slash != -1:
                    segment = input_buf[:next_slash]
                    input_buf = input_buf[next_slash:]
                else:
                    segment = input_buf
                    input_buf = ""
            output_buf.append(segment)

    resolved = "".join(output_buf)
    if not resolved.startswith("/"):
        resolved = "/" + resolved
    return resolved


def is_signed_or_sensitive_query(query: str) -> bool:
    """Check if query contains any parameter key matching K_signed."""
    if not query:
        return False
    try:
        pairs = parse_qsl(query, keep_blank_values=True)
        for k, _ in pairs:
            if k.lower() in SIGNED_QUERY_KEYS:
                return True
    except Exception:
        pass
    return False


def normalize_query_ordered(query: str) -> str:
    """
    Preserves exact query parameter ordering and duplicate keys.
    Decodes unreserved characters and normalizes hex to uppercase,
    while preserving '%2B' to distinguish plus from space.
    """
    if not query:
        return ""

    if is_signed_or_sensitive_query(query):
        # Protected signed query: retain byte-for-byte exact original string
        return query

    # Parse query while preserving order and blanks
    pairs = parse_qsl(query, keep_blank_values=True)
    if not pairs:
        return query

    normalized_pairs: List[Tuple[str, str]] = []
    for k, v in pairs:
        norm_k = normalize_percent_escapes(k, is_query=True)
        norm_v = normalize_percent_escapes(v, is_query=True)
        normalized_pairs.append((norm_k, norm_v))

    return urlencode(normalized_pairs, doseq=True)


class CanonicalURLNormalizer:
    """
    Deterministic RFC 3986 & LinkPilot v1.0.3 URL Normalizer.
    Enforces the Eight-Step Operation Order and Three-Entity Identity Rules.
    """

    @staticmethod
    def normalize_url(
        raw_url: str,
        base_url: Optional[str] = None,
        is_spa_domain_confirmed: bool = False
    ) -> NormalizedURLResult:
        if not raw_url or not isinstance(raw_url, str):
            return NormalizedURLResult(
                raw_url=raw_url or "",
                normalized_url="",
                observed_page_key="",
                scheme="",
                hostname="",
                port=None,
                path="",
                query="",
                fragment="",
                domain=classify_domain_5tuple(""),
                is_valid_web_url=False,
                error_message="Empty or invalid raw URL string",
            )

        stripped = raw_url.strip()

        # Reject excluded pseudo-protocols
        lower_raw = stripped.lower()
        if lower_raw.startswith(("javascript:", "mailto:", "tel:", "data:", "file:", "ftp:", "about:")):
            return NormalizedURLResult(
                raw_url=stripped,
                normalized_url="",
                observed_page_key="",
                scheme=lower_raw.split(":")[0],
                hostname="",
                port=None,
                path="",
                query="",
                fragment="",
                domain=classify_domain_5tuple(""),
                is_valid_web_url=False,
                error_message=f"Non-web pseudo-protocol rejected: {lower_raw.split(':')[0]}",
            )

        # 1. Base URL resolution
        if base_url:
            resolved = urljoin(base_url.strip(), stripped)
        else:
            resolved = stripped

        # Reject IPv6 Zone Identifier (%eth0 or %25eth0) in host
        if "[" in resolved and "]" in resolved:
            host_bracket = resolved[resolved.find("["):resolved.find("]")+1]
            if "%" in host_bracket:
                return NormalizedURLResult(
                    raw_url=stripped,
                    normalized_url="",
                    observed_page_key="",
                    scheme="",
                    hostname="",
                    port=None,
                    path="",
                    query="",
                    fragment="",
                    domain=classify_domain_5tuple(""),
                    is_valid_web_url=False,
                    error_message="IPv6 zone identifier rejected in web URI",
                )

        # 2. Syntax validation of percent-escapes (Step 1)
        if not validate_percent_encoding_syntax(resolved):
            return NormalizedURLResult(
                raw_url=stripped,
                normalized_url="",
                observed_page_key="",
                scheme="",
                hostname="",
                port=None,
                path="",
                query="",
                fragment="",
                domain=classify_domain_5tuple(""),
                is_valid_web_url=False,
                error_message="Invalid percent-escape syntax in URL",
            )

        try:
            parts = urlsplit(resolved)
        except Exception as e:
            return NormalizedURLResult(
                raw_url=stripped,
                normalized_url="",
                observed_page_key="",
                scheme="",
                hostname="",
                port=None,
                path="",
                query="",
                fragment="",
                domain=classify_domain_5tuple(""),
                is_valid_web_url=False,
                error_message=f"URL parsing failed: {e}",
            )

        # 3. Scheme normalization
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            return NormalizedURLResult(
                raw_url=stripped,
                normalized_url="",
                observed_page_key="",
                scheme=scheme,
                hostname="",
                port=None,
                path="",
                query="",
                fragment="",
                domain=classify_domain_5tuple(""),
                is_valid_web_url=False,
                error_message=f"Invalid web scheme (expected http or https): '{scheme}'",
            )

        # 4. Hostname & Port extraction
        raw_netloc = parts.netloc
        if not raw_netloc:
            return NormalizedURLResult(
                raw_url=stripped,
                normalized_url="",
                observed_page_key="",
                scheme=scheme,
                hostname="",
                port=None,
                path="",
                query="",
                fragment="",
                domain=classify_domain_5tuple(""),
                is_valid_web_url=False,
                error_message="Missing netloc/hostname in URL",
            )

        try:
            port = parts.port
        except ValueError as e:
            return NormalizedURLResult(
                raw_url=stripped,
                normalized_url="",
                observed_page_key="",
                scheme=scheme,
                hostname="",
                port=None,
                path="",
                query="",
                fragment="",
                domain=classify_domain_5tuple(""),
                is_valid_web_url=False,
                error_message=f"Invalid port in URL: {e}",
            )

        hostname = parts.hostname or ""
        if not hostname:
            return NormalizedURLResult(
                raw_url=stripped,
                normalized_url="",
                observed_page_key="",
                scheme=scheme,
                hostname="",
                port=None,
                path="",
                query="",
                fragment="",
                domain=classify_domain_5tuple(""),
                is_valid_web_url=False,
                error_message="Missing hostname in netloc",
            )

        # Validate Port
        if port is not None:
            if port <= 0 or port > 65535:
                return NormalizedURLResult(
                    raw_url=stripped,
                    normalized_url="",
                    observed_page_key="",
                    scheme=scheme,
                    hostname="",
                    port=None,
                    path="",
                    query="",
                    fragment="",
                    domain=classify_domain_5tuple(""),
                    is_valid_web_url=False,
                    error_message=f"Invalid port number: {port}",
                )

        # Validate Hostname syntax (max length 253, label max 63)
        if len(hostname) > 253 or any(len(label) > 63 for label in hostname.split(".")):
            return NormalizedURLResult(
                raw_url=stripped,
                normalized_url="",
                observed_page_key="",
                scheme=scheme,
                hostname="",
                port=None,
                path="",
                query="",
                fragment="",
                domain=classify_domain_5tuple(""),
                is_valid_web_url=False,
                error_message=f"Hostname exceeds RFC length limits: '{hostname[:30]}...'",
            )

        # Reject IPv6 Zone Identifier (%eth0)
        if "%" in hostname:
            return NormalizedURLResult(
                raw_url=stripped,
                normalized_url="",
                observed_page_key="",
                scheme=scheme,
                hostname="",
                port=None,
                path="",
                query="",
                fragment="",
                domain=classify_domain_5tuple(""),
                is_valid_web_url=False,
                error_message="IPv6 zone identifier rejected in web URI",
            )

        domain_info = classify_domain_5tuple(hostname)
        norm_hostname = domain_info.hostname

        # Default port removal
        effective_port = port
        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            effective_port = None

        if effective_port is not None:
            netloc = f"{norm_hostname}:{effective_port}"
        else:
            netloc = norm_hostname

        # 5. Path normalization (Steps 3, 4, 5, 6, 7)
        raw_path = parts.path or "/"
        # Normalize percent escapes in path (unreserved decoded, %2E preserved, %2F preserved, uppercase hex)
        escaped_path = normalize_percent_escapes(raw_path, is_query=False)
        # RFC 3986 dot-segment removal
        norm_path = rfc3986_remove_dot_segments(escaped_path)
        # Consecutive slashes are strictly PRESERVED per v1.0.3 spec
        if not norm_path.startswith("/"):
            norm_path = "/" + norm_path

        # 6. Query string normalization (Step 2: signed query isolation)
        is_signed = is_signed_or_sensitive_query(parts.query)
        if is_signed:
            norm_query = parts.query  # isolated byte-for-byte
        else:
            norm_query = normalize_query_ordered(parts.query)

        # 7. Construct Tier 2 normalized_url (always fragment-free server resource)
        normalized_url = urlunsplit((scheme, netloc, norm_path, norm_query, ""))

        # 8. SPA Hash Routing & Tier 3 observed_page_key
        raw_fragment = parts.fragment
        spa_telemetry = SPARouteTelemetry.STANDARD_FRAGMENT
        is_authoritative = True
        observed_page_key = normalized_url

        if raw_fragment:
            if raw_fragment.startswith(("/", "!/")):
                if is_spa_domain_confirmed:
                    spa_telemetry = SPARouteTelemetry.HASH_ROUTE_CONFIRMED
                    observed_page_key = f"{normalized_url}#{raw_fragment}"
                    is_authoritative = True
                else:
                    spa_telemetry = SPARouteTelemetry.HASH_ROUTE_SUSPECTED
                    observed_page_key = f"{normalized_url}#{raw_fragment}"
                    is_authoritative = False  # Unscored observation pending confirmation
            else:
                spa_telemetry = SPARouteTelemetry.STANDARD_FRAGMENT
                observed_page_key = normalized_url

        return NormalizedURLResult(
            raw_url=stripped,
            normalized_url=normalized_url,
            observed_page_key=observed_page_key,
            scheme=scheme,
            hostname=norm_hostname,
            port=effective_port,
            path=norm_path,
            query=norm_query,
            fragment=raw_fragment,
            domain=domain_info,
            is_valid_web_url=True,
            spa_telemetry=spa_telemetry,
            is_authoritative_scored_page=is_authoritative,
            error_message=None,
        )


# ==============================================================================
# CANONICAL DECLARATIONS & GRAPH-WIDE TOPOLOGY RESOLUTION
# ==============================================================================

@dataclass
class CanonicalDeclarationEvidence:
    """Record of an individual physical canonical declaration."""
    source_url: str
    raw_target_value: str
    declaration_source: str           # "HTML_TAG" or "HTTP_LINK_HEADER"
    resolved_target_url: Optional[str]
    is_valid: bool
    error_message: Optional[str] = None


@dataclass
class CanonicalResolutionSummary:
    """Evaluated canonical assessment across the three classification dimensions."""
    source_url: str
    shape: DeclarationShape
    relationship: TargetRelationship
    topology: TopologyClassification
    effective_target_url: str
    status: CanonicalResolutionStatus
    canonical_cluster_id: str


def compute_canonical_cluster_id(site_boundary: str, effective_target_url: str) -> str:
    """
    Computes deterministic persistent canonical cluster ID using versioned SHA-256.
    Python built-in hash() is strictly prohibited.
    """
    token = f"linkpilot:v1:{site_boundary}:{effective_target_url}".encode("utf-8")
    return hashlib.sha256(token).hexdigest()[:16]


def parse_rfc8288_link_headers(header_value: str) -> List[str]:
    """
    Standards-aware RFC 8288 HTTP Link header parser for rel="canonical".
    Handles multiple comma-separated links, quoted and unquoted parameters.
    """
    if not header_value:
        return []

    results: List[str] = []
    # Split by commas that are outside angle brackets
    links: List[str] = []
    current: List[str] = []
    in_bracket = False
    for char in header_value:
        if char == "<":
            in_bracket = True
            current.append(char)
        elif char == ">":
            in_bracket = False
            current.append(char)
        elif char == "," and not in_bracket:
            links.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        links.append("".join(current).strip())

    for link in links:
        match = re.match(r"^<([^>]+)>(.*)$", link)
        if not match:
            continue
        uri = match.group(1).strip()
        params = match.group(2).strip()

        # Check for rel=canonical or rel="canonical"
        rel_match = re.search(r'rel\s*=\s*(?:"([^"]+)"|([^\s;]+))', params, re.IGNORECASE)
        if rel_match:
            rel_val = (rel_match.group(1) or rel_match.group(2)).lower()
            rel_tokens = set(rel_val.split())
            if "canonical" in rel_tokens:
                results.append(uri)

    return results


class TarjanSCC:
    """Graph-wide Strongly Connected Components (Tarjan's algorithm)."""

    def __init__(self, graph: Dict[str, List[str]]):
        self.graph = graph
        self.index = 0
        self.indices: Dict[str, int] = {}
        self.lowlinks: Dict[str, int] = {}
        self.on_stack: Set[str] = set()
        self.stack: List[str] = []
        self.sccs: List[List[str]] = []

    def run(self) -> List[List[str]]:
        for node in list(self.graph.keys()):
            if node not in self.indices:
                self._strongconnect(node)
        return self.sccs

    def _strongconnect(self, node: str):
        self.indices[node] = self.index
        self.lowlinks[node] = self.index
        self.index += 1
        self.stack.append(node)
        self.on_stack.add(node)

        for neighbor in self.graph.get(node, []):
            if neighbor not in self.indices:
                self._strongconnect(neighbor)
                self.lowlinks[node] = min(self.lowlinks[node], self.lowlinks[neighbor])
            elif neighbor in self.on_stack:
                self.lowlinks[node] = min(self.lowlinks[node], self.indices[neighbor])

        if self.lowlinks[node] == self.indices[node]:
            scc: List[str] = []
            while True:
                w = self.stack.pop()
                self.on_stack.remove(w)
                scc.append(w)
                if w == node:
                    break
            self.sccs.append(scc)


class CanonicalTargetResolver:
    """
    Evaluates canonical declarations across HTML tags and HTTP Link headers.
    Applies the Three-Dimensional Precedence Rules and graph-wide Tarjan SCC cycle resolution.
    """

    @classmethod
    def evaluate_page_canonical(
        cls,
        source_url: str,
        html_declarations: Optional[List[str]] = None,
        http_link_headers: Optional[List[str]] = None,
        effective_base_url: Optional[str] = None
    ) -> CanonicalResolutionSummary:
        norm_source = CanonicalURLNormalizer.normalize_url(source_url)
        clean_source = norm_source.normalized_url
        site_boundary = norm_source.domain.site_boundary

        base_url = effective_base_url or clean_source
        evidences: List[CanonicalDeclarationEvidence] = []

        # 1. Parse HTML tags
        if html_declarations:
            for raw_decl in html_declarations:
                norm_decl = CanonicalURLNormalizer.normalize_url(raw_decl, base_url=base_url)
                evidences.append(
                    CanonicalDeclarationEvidence(
                        source_url=clean_source,
                        raw_target_value=raw_decl,
                        declaration_source="HTML_TAG",
                        resolved_target_url=norm_decl.normalized_url if norm_decl.is_valid_web_url else None,
                        is_valid=norm_decl.is_valid_web_url,
                        error_message=norm_decl.error_message,
                    )
                )

        # 2. Parse HTTP headers
        if http_link_headers:
            for header_val in http_link_headers:
                parsed_links = parse_rfc8288_link_headers(header_val)
                for raw_link in parsed_links:
                    norm_link = CanonicalURLNormalizer.normalize_url(raw_link, base_url=base_url)
                    evidences.append(
                        CanonicalDeclarationEvidence(
                            source_url=clean_source,
                            raw_target_value=raw_link,
                            declaration_source="HTTP_LINK_HEADER",
                            resolved_target_url=norm_link.normalized_url if norm_link.is_valid_web_url else None,
                            is_valid=norm_link.is_valid_web_url,
                            error_message=norm_link.error_message,
                        )
                    )

        # 3. Classify Declaration Shape
        if not evidences:
            return CanonicalResolutionSummary(
                source_url=clean_source,
                shape=DeclarationShape.NONE,
                relationship=TargetRelationship.SELF,
                topology=TopologyClassification.DIRECT,
                effective_target_url=clean_source,
                status=CanonicalResolutionStatus.NO_CANONICAL,
                canonical_cluster_id=compute_canonical_cluster_id(site_boundary, clean_source),
            )

        has_valid = any(e.is_valid for e in evidences)
        has_invalid = any(not e.is_valid for e in evidences)

        if has_valid and has_invalid:
            # PARTIALLY_INVALID: Reject all; fallback to self
            return CanonicalResolutionSummary(
                source_url=clean_source,
                shape=DeclarationShape.PARTIALLY_INVALID,
                relationship=TargetRelationship.SELF,
                topology=TopologyClassification.DIRECT,
                effective_target_url=clean_source,
                status=CanonicalResolutionStatus.INVALID_REJECTED,
                canonical_cluster_id=compute_canonical_cluster_id(site_boundary, clean_source),
            )

        if not has_valid:
            # INVALID: Reject target; fallback to self
            return CanonicalResolutionSummary(
                source_url=clean_source,
                shape=DeclarationShape.INVALID,
                relationship=TargetRelationship.SELF,
                topology=TopologyClassification.DIRECT,
                effective_target_url=clean_source,
                status=CanonicalResolutionStatus.INVALID_REJECTED,
                canonical_cluster_id=compute_canonical_cluster_id(site_boundary, clean_source),
            )

        # All declarations are valid: inspect distinct resolved target URLs
        distinct_targets = {e.resolved_target_url for e in evidences if e.resolved_target_url}

        if len(distinct_targets) > 1:
            # MULTIPLE_CONFLICTING: Reject all; fallback to self
            return CanonicalResolutionSummary(
                source_url=clean_source,
                shape=DeclarationShape.MULTIPLE_CONFLICTING,
                relationship=TargetRelationship.SELF,
                topology=TopologyClassification.DIRECT,
                effective_target_url=clean_source,
                status=CanonicalResolutionStatus.CANONICAL_CONFLICT_REJECTED,
                canonical_cluster_id=compute_canonical_cluster_id(site_boundary, clean_source),
            )

        # Exactly one unique target URL declared
        unique_target = list(distinct_targets)[0]
        shape = DeclarationShape.SINGLE if len(evidences) == 1 else DeclarationShape.MULTIPLE_IDENTICAL

        # 4. Classify Target Relationship
        if unique_target == clean_source:
            # Self-canonical affirmation (A -> A)
            return CanonicalResolutionSummary(
                source_url=clean_source,
                shape=shape,
                relationship=TargetRelationship.SELF,
                topology=TopologyClassification.DIRECT,
                effective_target_url=clean_source,
                status=CanonicalResolutionStatus.SELF_CANONICAL,
                canonical_cluster_id=compute_canonical_cluster_id(site_boundary, clean_source),
            )

        target_norm = CanonicalURLNormalizer.normalize_url(unique_target)
        target_site_boundary = target_norm.domain.site_boundary

        if target_site_boundary != site_boundary:
            # CROSS_SITE_BOUNDARY: metadata recorded, firewalled from internal graph
            return CanonicalResolutionSummary(
                source_url=clean_source,
                shape=shape,
                relationship=TargetRelationship.CROSS_SITE_BOUNDARY,
                topology=TopologyClassification.DIRECT,
                effective_target_url=unique_target,
                status=CanonicalResolutionStatus.CROSS_DOMAIN_FIREWALLED,
                canonical_cluster_id=compute_canonical_cluster_id(site_boundary, clean_source),
            )

        # SAME_SITE_BOUNDARY: Direct candidate
        return CanonicalResolutionSummary(
            source_url=clean_source,
            shape=shape,
            relationship=TargetRelationship.SAME_SITE_BOUNDARY,
            topology=TopologyClassification.DIRECT,
            effective_target_url=unique_target,
            status=CanonicalResolutionStatus.CANONICAL_RESOLVED,
            canonical_cluster_id=compute_canonical_cluster_id(site_boundary, unique_target),
        )

    @classmethod
    def resolve_graph_wide_canonicals(
        cls, page_summaries: Dict[str, CanonicalResolutionSummary]
    ) -> Dict[str, CanonicalResolutionSummary]:
        """
        Executes graph-wide canonical condensation:
        1. Builds internal preference graph over SAME_SITE_BOUNDARY edges.
        2. Detects strongly connected components via Tarjan SCC.
        3. Identifies multi-node cycles (|SCC| >= 2); breaks cycles (revert to self).
        4. Rejects upstream aliases that enter cycles.
        5. Condenses acyclic chains (A -> B -> C to terminal C).
        """
        # Build adjacency graph
        pref_graph: Dict[str, List[str]] = {}
        for src, summary in page_summaries.items():
            if summary.relationship == TargetRelationship.SAME_SITE_BOUNDARY and summary.effective_target_url != src:
                pref_graph.setdefault(src, []).append(summary.effective_target_url)
            else:
                pref_graph.setdefault(src, [])

        # Add all targets as nodes in pref_graph
        for targets in list(pref_graph.values()):
            for tgt in targets:
                pref_graph.setdefault(tgt, [])

        # Run Tarjan SCC
        scc_detector = TarjanSCC(pref_graph)
        sccs = scc_detector.run()

        # Identify cycle members
        cycle_nodes: Set[str] = set()
        for scc in sccs:
            if len(scc) >= 2:
                for node in scc:
                    cycle_nodes.add(node)

        # Updated summaries
        resolved: Dict[str, CanonicalResolutionSummary] = dict(page_summaries)

        # Handle cycle members: all members revert to self (no arbitrary winner)
        for node in cycle_nodes:
            if node in resolved:
                old = resolved[node]
                resolved[node] = CanonicalResolutionSummary(
                    source_url=old.source_url,
                    shape=old.shape,
                    relationship=old.relationship,
                    topology=TopologyClassification.CYCLE,
                    effective_target_url=old.source_url,
                    status=CanonicalResolutionStatus.CANONICAL_CYCLE_REJECTED,
                    canonical_cluster_id=compute_canonical_cluster_id(
                        CanonicalURLNormalizer.normalize_url(old.source_url).domain.site_boundary,
                        old.source_url
                    ),
                )

        # Handle upstream aliases entering a cycle
        for src, summary in list(resolved.items()):
            if summary.effective_target_url in cycle_nodes and src not in cycle_nodes:
                resolved[src] = CanonicalResolutionSummary(
                    source_url=summary.source_url,
                    shape=summary.shape,
                    relationship=summary.relationship,
                    topology=TopologyClassification.UPSTREAM_ALIAS_TO_CYCLE,
                    effective_target_url=summary.source_url,
                    status=CanonicalResolutionStatus.UPSTREAM_ALIAS_REJECTED,
                    canonical_cluster_id=compute_canonical_cluster_id(
                        CanonicalURLNormalizer.normalize_url(summary.source_url).domain.site_boundary,
                        summary.source_url
                    ),
                )

        # Condense acyclic chains (A -> B -> C)
        for src, summary in list(resolved.items()):
            if (
                summary.relationship == TargetRelationship.SAME_SITE_BOUNDARY
                and summary.topology == TopologyClassification.DIRECT
            ):
                chain: List[str] = [src]
                curr = summary.effective_target_url
                visited = {src}
                while curr in pref_graph and curr not in visited and curr not in cycle_nodes:
                    visited.add(curr)
                    chain.append(curr)
                    if curr in resolved and resolved[curr].effective_target_url != curr:
                        curr = resolved[curr].effective_target_url
                    else:
                        break

                if len(chain) >= 3:
                    # Flatten chain to terminal node
                    terminal = chain[-1]
                    site_boundary = CanonicalURLNormalizer.normalize_url(src).domain.site_boundary
                    resolved[src] = CanonicalResolutionSummary(
                        source_url=src,
                        shape=summary.shape,
                        relationship=summary.relationship,
                        topology=TopologyClassification.CHAIN,
                        effective_target_url=terminal,
                        status=CanonicalResolutionStatus.CANONICAL_CHAIN_FLATTENED,
                        canonical_cluster_id=compute_canonical_cluster_id(site_boundary, terminal),
                    )

        return resolved

    # Compatibility method for Milestone 1 test signatures
    @classmethod
    def resolve_canonical(cls, url: str, declared_canonical: Optional[str]) -> str:
        if not declared_canonical:
            return CanonicalURLNormalizer.normalize_url(url).normalized_url
        res = cls.evaluate_page_canonical(url, html_declarations=[declared_canonical])
        return res.effective_target_url


# ==============================================================================
# PHASE 1A LEGACY DATABASE ADAPTER
# ==============================================================================

class LegacyURLAdapter:
    """
    Read-only in-memory adapter for Phase 1A database records.
    Maps legacy pages.normalized_url records without rewriting production tables.
    """

    @staticmethod
    def adapt_page_record(page_row: Dict[str, Any]) -> NormalizedURLResult:
        legacy_norm_url = page_row.get("normalized_url", "")
        raw_url = page_row.get("raw_url") or legacy_norm_url

        norm_result = CanonicalURLNormalizer.normalize_url(legacy_norm_url)
        return norm_result

    @staticmethod
    def detect_lossy_legacy_normalization(legacy_url: str, raw_url: Optional[str]) -> ProvenanceFidelityStatus:
        if not raw_url:
            return ProvenanceFidelityStatus.LEGACY_NORMALIZATION_LOSSY
        if legacy_url == raw_url:
            return ProvenanceFidelityStatus.EXACT_RAW_OBSERVATION
        return ProvenanceFidelityStatus.PARSER_DERIVED
