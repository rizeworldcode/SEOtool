"""
LinkPilot AI — Automated Test Suite for On-Demand Single-URL Fetcher,
Mini-Crawler, and Interactive Web UI / REST API.
tests/test_ondemand_and_web.py
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Dict
import urllib.request
import urllib.error

import httpx
import pytest
from typer.testing import CliRunner

from app.authority.ondemand import (
    OnDemandAuthorityAnalyzer,
    check_url_da_pa,
    normalize_user_target,
)
from app.commands.cli import app as cli_app
from app.web.server import handle_web_request
from tests.governance.test_database_integrity import DATABASE_LINEAGE


runner = CliRunner()


def compute_db_sha256(path: str = "./data/linkpilot.db") -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()


# ==============================================================================
# 1. URL NORMALIZATION & TARGET RESOLUTION TESTS
# ==============================================================================

def test_normalize_user_target_variants():
    """OD-01: Verify bare domains, subdomains, and full URLs normalize deterministically."""
    t_url, t_key, r_url, dom = normalize_user_target("WWW.ZONIRAZ.COM")
    assert dom == "zoniraz.com"
    assert t_url == "https://www.zoniraz.com/"
    assert r_url == "https://www.zoniraz.com/"

    t_url2, _, r_url2, dom2 = normalize_user_target("https://blog.example.co.uk/articles/seo-guide?utm_source=x#top")
    assert dom2 == "example.co.uk"
    assert "articles/seo-guide" in t_url2
    assert r_url2 == "https://blog.example.co.uk/"

    with pytest.raises(ValueError):
        normalize_user_target("   ")


# ==============================================================================
# 2. ON-DEMAND MINI-CRAWLER & ARCHIVE INTEGRATION (MOCK HTTP TRANSPORT)
# ==============================================================================

def test_ondemand_live_mini_crawler_and_archive_scoring():
    """
    OD-02: Verify full live HTML mini-crawl + Wayback CDX archive integration +
    LP-PA and LP-DA mathematical calculation for an arbitrary external website.
    """
    pages_html: Dict[str, str] = {
        "https://acme-analytics.io/": """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <title>Acme Analytics — Enterprise Search Telemetry</title>
                <meta name="description" content="Real-time graph authority analytics." />
                <link rel="canonical" href="https://acme-analytics.io/" />
            </head>
            <body>
                <header>
                    <nav>
                        <a href="/platform">Platform</a>
                        <a href="/pricing">Pricing</a>
                        <a href="/docs">Documentation</a>
                    </nav>
                </header>
                <main>
                    <h1>Enterprise Search Telemetry</h1>
                    <article>
                        <a href="/blog/pagerank-math">PageRank Mathematics Deep Dive</a>
                        <a href="https://ietf.org/rfc/rfc3986.txt">RFC 3986 Standard</a>
                        <a href="https://partner-ads.example.net/" rel="nofollow sponsored">Sponsor</a>
                    </article>
                </main>
                <footer>
                    <a href="/about">About Us</a>
                </footer>
            </body>
            </html>
        """,
        "https://acme-analytics.io/platform": """
            <html><head><title>Platform — Acme</title></head>
            <body>
                <nav><a href="/">Home</a><a href="/docs">Docs</a></nav>
                <main><h1>Platform Architecture</h1><a href="/blog/pagerank-math">Math Guide</a></main>
            </body></html>
        """,
        "https://acme-analytics.io/pricing": """
            <html><head><title>Pricing — Acme</title></head>
            <body>
                <nav><a href="/">Home</a><a href="/platform">Platform</a></nav>
                <main><h1>Pricing Plans</h1></main>
            </body></html>
        """,
        "https://acme-analytics.io/docs": """
            <html><head><title>Docs — Acme</title></head>
            <body>
                <nav><a href="/">Home</a><a href="/platform">Platform</a></nav>
                <main><h1>Developer Documentation</h1><a href="https://github.com/acme/sdk" rel="ugc">SDK Repo</a></main>
            </body></html>
        """,
        "https://acme-analytics.io/blog/pagerank-math": """
            <html><head><title>PageRank Math — Acme</title></head>
            <body>
                <nav><a href="/">Home</a><a href="/docs">Docs</a></nav>
                <article><h1>Stationary Distributions</h1></article>
            </body></html>
        """,
    }

    wayback_cdx_payload = [
        ["timestamp", "original", "mimetype", "statuscode"],
        ["20190512100000", "https://acme-analytics.io/", "text/html", "200"],
        ["20210814120000", "https://acme-analytics.io/case-studies", "text/html", "200"],
        ["20231102153000", "https://acme-analytics.io/research-paper", "text/html", "200"],
        ["20240419091500", "https://acme-analytics.io/whitepaper", "text/html", "200"],
    ]

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "web.archive.org/cdx/search/cdx" in url_str:
            return httpx.Response(200, json=wayback_cdx_payload)
        if url_str in pages_html:
            return httpx.Response(
                200,
                text=pages_html[url_str],
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        return httpx.Response(404, text="Not Found")

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    analyzer = OnDemandAuthorityAnalyzer(
        db_path="./data/linkpilot.db",
        max_internal_pages=5,
        enable_live_crawl=True,
        enable_archive_lookup=True,
        http_client=mock_client,
    )
    report = analyzer.analyze("https://acme-analytics.io/")

    assert report.status == "COMPLETED"
    assert report.domain_name == "acme-analytics.io"
    assert 15.0 <= report.lp_da_display <= 95.0
    assert 15.0 <= report.target_pa_display <= 95.0
    assert report.level1_audit_passed is True
    assert report.scored_pages_count >= 6
    assert report.pages_crawled_live >= 4
    assert report.pages_from_archives >= 2
    assert report.earliest_archive_year == 2019
    assert report.target_title == "Acme Analytics — Enterprise Search Telemetry"
    assert "Enterprise Search Telemetry" in report.target_h1_headings
    assert report.internal_links_count >= 8
    assert report.external_outbound_links_count >= 2
    assert report.nofollow_links_count + report.sponsored_links_count >= 1
    assert report.ugc_links_count >= 1
    assert len(report.top_constituent_pages) == report.scored_pages_count


# ==============================================================================
# 3. LOCAL DATABASE GRAPH & SUBPAGE PA vs DOMAIN DA CONSISTENCY
# ==============================================================================

def test_ondemand_local_db_domain_and_subpage_differentiation():
    """
    OD-03: Verify checking root URL vs internal subpage on zoniraz.com yields
    Moz-calibrated Domain Authority (DA ~ 11.0, filtering out synthetic Phase-1A mock fixtures)
    while accurately differentiating Page Authority (Homepage PA > Subpage PA).
    """
    root_rep = check_url_da_pa(
        "https://www.zoniraz.com/",
        enable_live_crawl=False,
        enable_archive_lookup=False,
    )
    assert root_rep.status == "COMPLETED"
    assert root_rep.domain_name == "zoniraz.com"
    assert root_rep.scored_pages_count == 115
    assert 8.0 <= root_rep.lp_da_display <= 15.0
    assert 15.0 <= root_rep.target_pa_display <= 25.0
    assert root_rep.target_page_rank_on_domain == 1
    assert root_rep.level1_audit_passed is True

    # Pick a lower-ranked constituent page from the domain
    subpage_url = root_rep.top_constituent_pages[-1]["normalized_url"]
    sub_rep = check_url_da_pa(
        subpage_url,
        enable_live_crawl=False,
        enable_archive_lookup=False,
    )
    assert sub_rep.status == "COMPLETED"
    assert sub_rep.domain_name == "zoniraz.com"
    assert sub_rep.lp_da_display == root_rep.lp_da_display
    # Root page PA should be strictly higher than a leaf subpage PA
    assert root_rep.target_pa_display > sub_rep.target_pa_display
    assert sub_rep.target_page_rank_on_domain > 1


def test_ondemand_monotonicity_across_site_profiles():
    """
    OD-04: Verify Moz-calibrated authority monotonicity across global tiers:
      github.com (DA 96) > arxiv.org (DA 91) > commoncrawl.org (DA 68) >
      zoniraz.com (DA 11) > brand-new-unindexed-test.io (DA 2).
    """
    rep_flagship = check_url_da_pa("github.com", enable_live_crawl=False, enable_archive_lookup=False)
    rep_academic = check_url_da_pa("arxiv.org", enable_live_crawl=False, enable_archive_lookup=False)
    rep_mid = check_url_da_pa("commoncrawl.org", enable_live_crawl=False, enable_archive_lookup=False)
    rep_niche = check_url_da_pa("zoniraz.com", enable_live_crawl=False, enable_archive_lookup=False)
    rep_min = check_url_da_pa("brand-new-unindexed-test.io", enable_live_crawl=False, enable_archive_lookup=False)

    assert (
        rep_flagship.lp_da_display
        > rep_academic.lp_da_display
        > rep_mid.lp_da_display
        > rep_niche.lp_da_display
        > rep_min.lp_da_display
        > 0.0
    )
    assert (
        rep_flagship.level1_audit_passed
        and rep_academic.level1_audit_passed
        and rep_mid.level1_audit_passed
        and rep_niche.level1_audit_passed
        and rep_min.level1_audit_passed
    )


def test_ondemand_moz_5factor_spam_penalty_and_page_specific_pa():
    """
    OD-08: Verify Moz 5-Factor Zero-API-Key Engine:
      - Factor 2: MozTrust score differentiation (.gov/.edu vs normal vs spam TLD)
      - Factor 4: Moz Spam Score (1%-100%) detects hyphen/digit/spam-TLD/thin-HTML/link-farm
                  signals and applies a DA/PA spam penalty
      - Factor 5: Page-specific Wayback CDX archive snapshots boost a specific subpage's PA
    """
    def mock_5factor_handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "web.archive.org/cdx/search/cdx" in u:
            if "popular-tool" in u and "showNumPages=true" in u:
                return httpx.Response(200, text="15")
            if "popular-tool" in u:
                return httpx.Response(200, json=[
                    ["timestamp", "original", "mimetype", "statuscode"],
                    *[[f"20210{(i%9)+1}10120000", "https://clean-brand.org/popular-tool", "text/html", "200"] for i in range(20)],
                ])
            if "showNumPages=true" in u:
                return httpx.Response(200, text="10")
            return httpx.Response(200, json=[
                ["timestamp", "original", "mimetype", "statuscode"],
                ["20190101000000", "https://clean-brand.org/", "text/html", "200"],
                ["20200101000000", "https://clean-brand.org/popular-tool", "text/html", "200"],
                ["20210101000000", "https://clean-brand.org/quiet-page", "text/html", "200"],
            ])

        if "buy-cheap-casino-247.xyz" in u:
            # Spammy page: missing title, meta keywords present, many commercial spam outbound links
            spam_links = "".join(f"<a href='https://casino-ref-{i}.biz/'>casino payday loan free money</a>" for i in range(30))
            return httpx.Response(
                200,
                text=f"<html><head><meta name='keywords' content='casino,loan'/></head><body>{spam_links}</body></html>",
                headers={"Content-Type": "text/html"},
            )

        if "clean-brand.org" in u:
            return httpx.Response(
                200,
                text="""
                <html lang="en">
                <head>
                    <title>Clean Brand Organization — Official Portal</title>
                    <meta name="description" content="Trusted open-source research and engineering tools." />
                    <link rel="canonical" href="https://clean-brand.org/" />
                    <link rel="icon" href="/favicon.ico" />
                    <link rel="apple-touch-icon" href="/apple-touch-icon.png" />
                    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter&display=swap" />
                    <script src="https://www.googletagmanager.com/gtm.js?id=GTM-CLEAN123"></script>
                </head>
                <body>
                    <h1>Clean Brand Research Portal</h1>
                    <p><strong>Comprehensive documentation</strong> and engineering datasets for modern web analysis and link equity research.</p>
                    <p>Contact us at <a href="mailto:hello@clean-brand.org">hello@clean-brand.org</a> or <a href="tel:+1-555-234-5678">+1-555-234-5678</a>.</p>
                    <nav><a href="/popular-tool">Popular Tool</a><a href="/quiet-page">Quiet Page</a><a href="/about">About</a><a href="/contact">Contact</a></nav>
                    <footer><a href="https://github.com/clean-brand">GitHub</a><a href="https://linkedin.com/company/clean-brand">LinkedIn</a></footer>
                </body>
                </html>
                """,
                headers={"Content-Type": "text/html"},
            )
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_5factor_handler))

    analyzer = OnDemandAuthorityAnalyzer(
        enable_live_crawl=True,
        enable_archive_lookup=True,
        http_client=mock_client,
    )

    # 1. Clean .org site vs Spammy .xyz site with identical archive baseline
    clean_rep = analyzer.analyze("https://clean-brand.org/popular-tool")
    spam_rep = analyzer.analyze("http://buy-cheap-casino-247.xyz/")

    assert clean_rep.status == "COMPLETED"
    assert spam_rep.status == "COMPLETED"
    assert clean_rep.moz_spam_signals_checked == 27
    assert spam_rep.moz_spam_signals_checked == 27
    assert len(clean_rep.moz_spam_audit) == 27
    assert len(spam_rep.moz_spam_audit) == 27
    assert clean_rep.moz_spam_risk_level == "Low"
    assert clean_rep.moz_spam_score_pct <= 10.0
    assert spam_rep.moz_spam_risk_level == "High"
    assert spam_rep.moz_spam_score_pct >= 61.0
    assert spam_rep.moz_spam_penalty >= 0.20
    assert len(spam_rep.moz_spam_flags) >= 7
    assert clean_rep.moz_trust_score > spam_rep.moz_trust_score
    assert clean_rep.lp_da_display > spam_rep.lp_da_display
    assert clean_rep.target_page_archive_snapshots == 20
    assert clean_rep.level1_audit_passed and spam_rep.level1_audit_passed


def test_ondemand_repeated_checks_zero_variance_under_transient_timeout():
    """
    OD-09: Verify that checking a domain once when Wayback/HTML succeeds and then checking
    it a second time when Wayback/HTML experiences a transient timeout/503 returns the
    EXACT same DA and PA score (0.00 drift) thanks to the Monotonic Domain Signal Cache.
    """
    call_phase = {"attempt": 1}

    def flaky_network_handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if call_phase["attempt"] == 1:
            if "showNumPages=true" in u:
                return httpx.Response(200, text="42")
            if "web.archive.org/cdx/search/cdx" in u:
                return httpx.Response(200, json=[
                    ["timestamp", "original", "mimetype", "statuscode"],
                    ["20180310120000", "https://steady-domain-test.org/", "text/html", "200"],
                    ["20210615120000", "https://steady-domain-test.org/features", "text/html", "200"],
                ])
            return httpx.Response(
                200,
                text="<html><head><title>Steady Domain</title></head><body><main><a href='/features'>Features</a></main></body></html>",
                headers={"Content-Type": "text/html"},
            )
        else:
            # Attempt 2: Simulate Wayback 503 timeout / rate-limit and target site 503
            return httpx.Response(503, text="Service Temporarily Unavailable")

    flaky_client = httpx.Client(transport=httpx.MockTransport(flaky_network_handler))
    analyzer = OnDemandAuthorityAnalyzer(
        enable_live_crawl=True,
        enable_archive_lookup=True,
        http_client=flaky_client,
    )

    # Check #1 (Network succeeds)
    rep1 = analyzer.analyze("https://steady-domain-test.org/")
    assert rep1.status == "COMPLETED"
    assert rep1.lp_da_display > 40.0

    # Check #2 (Network returns 503 on everything — cache must prevent score drop!)
    call_phase["attempt"] = 2
    rep2 = analyzer.analyze("https://steady-domain-test.org/")
    assert rep2.status == "COMPLETED"
    assert rep2.lp_da_display == rep1.lp_da_display
    assert rep2.target_pa_display == rep1.target_pa_display
    assert rep2.scored_pages_count == rep1.scored_pages_count


def test_ondemand_ssrf_protection():
    """OD-05: Verify SSRF guard blocks private/loopback IP targets during live crawl."""
    rep = check_url_da_pa("http://127.0.0.1:8080/private", enable_live_crawl=True, enable_archive_lookup=False)
    assert rep.status == "FAILED"
    assert "SSRF" in (rep.error_message or "")


# ==============================================================================
# 4. INTERACTIVE WEB UI & REST API ENDPOINT TESTS
# ==============================================================================

def test_web_ui_and_rest_api_endpoints():
    """
    OD-06: Verify LinkPilot Web UI & REST API dispatcher:
      - GET / serves the Interactive HTML Dashboard with DA & PA gauge cards
      - GET /api/health returns status ok
      - GET /api/domains lists known database domains
      - POST /api/check evaluates DA & PA and returns complete JSON telemetry
      - GET /api/check?url=... evaluates DA & PA via query string
      - GET /api/history records the evaluated domain
    """
    # 1. GET / Dashboard HTML
    code, ctype, html = handle_web_request("GET", "/", db_path="./data/linkpilot.db")
    assert code == 200
    assert "text/html" in ctype
    assert ("On-Demand Domain &amp; Page Authority Checker" in html) or ("Domain &amp; Page Authority" in html)
    assert "Domain Authority" in html
    assert "Page Authority" in html

    # 2. GET /api/health
    code, _, health = handle_web_request("GET", "/api/health", db_path="./data/linkpilot.db")
    assert code == 200
    assert health["status"] == "ok"
    assert health["algorithm_version"] == "LP-DA-v1.0.0"

    # 3. GET /api/domains
    code, _, dom_data = handle_web_request("GET", "/api/domains", db_path="./data/linkpilot.db")
    assert code == 200
    assert dom_data["count"] > 0
    domain_names = [d["domain_name"] for d in dom_data["domains"]]
    assert "zoniraz.com" in domain_names

    # 4. POST /api/check
    req_payload = json.dumps({
        "url": "https://www.zoniraz.com",
        "max_internal_pages": 3,
        "enable_live_crawl": False,
        "enable_archive_lookup": False,
    })
    code, _, report = handle_web_request("POST", "/api/check", raw_body=req_payload, db_path="./data/linkpilot.db")
    assert code == 200
    assert report["status"] == "COMPLETED"
    assert report["domain_name"] == "zoniraz.com"
    assert 8.0 <= report["lp_da_display"] <= 15.0
    assert 15.0 <= report["target_pa_display"] <= 25.0
    assert report["scored_pages_count"] == 115
    assert len(report["top_constituent_pages"]) > 0

    # 5. GET /api/check?url=commoncrawl.org
    code, _, report_cc = handle_web_request(
        "GET",
        "/api/check?url=commoncrawl.org&live_crawl=false&archive=false",
        db_path="./data/linkpilot.db",
    )
    assert code == 200
    assert report_cc["status"] == "COMPLETED"
    assert report_cc["domain_name"] == "commoncrawl.org"
    assert report_cc["scored_pages_count"] == 19

    # 6. GET /api/history
    code, _, hist = handle_web_request("GET", "/api/history", db_path="./data/linkpilot.db")
    assert code == 200
    assert hist["count"] >= 2
    assert hist["history"][0]["domain_name"] == "commoncrawl.org"


def test_footer_inside_pages():
    """Verify that inside footer subpages render with matching theme and 200 OK."""
    from app.web.pages import FOOTER_PAGES

    assert len(FOOTER_PAGES) >= 15
    assert "/authority-models" not in FOOTER_PAGES
    for path in FOOTER_PAGES:
        code, ctype, body = handle_web_request("GET", path, db_path="./data/linkpilot.db")
        assert code == 200, f"Expected 200 for {path}, got {code}"
        assert "text/html" in ctype
        assert "RIZEMETRICS" in body
        assert "curved-hero-section" in body
        assert "site-footer" in body


# ==============================================================================
# 5. CLI 'authority check' & DATABASE INVARIANCE VERIFICATION
# ==============================================================================

def test_cli_authority_check_and_db_invariance():
    """
    OD-07: Verify 'authority check' CLI command succeeds and leaves ./data/linkpilot.db
    bit-for-bit identical to DATABASE_LINEAGE['post_m5_baseline'].
    """
    sha_before = compute_db_sha256("./data/linkpilot.db")
    assert sha_before == DATABASE_LINEAGE["post_m5_baseline"]

    res = runner.invoke(
        cli_app,
        ["authority", "check", "https://www.zoniraz.com", "--no-live-crawl", "--no-archive", "--show-pages", "5"],
    )
    assert res.exit_code == 0
    assert "LinkPilot On-Demand DA / PA Report: zoniraz.com" in res.output
    assert "Domain Authority (LP-DA 0-100)" in res.output
    assert "Page Authority (LP-PA 0-100)" in res.output

    sha_after = compute_db_sha256("./data/linkpilot.db")
    assert sha_after == DATABASE_LINEAGE["post_m5_baseline"]
