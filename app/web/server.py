"""
LinkPilot AI — Interactive Web Dashboard & On-Demand DA/PA Checker Server.
app/web/server.py

Provides:
  - Interactive Browser UI (GET /) with radial gauge cards for DA (0-100) and
    PA (0-100), link profile telemetry, top constituent pages table, inbound/outbound
    link explorer, and mathematical formula verification.
  - JSON REST API:
      GET  /api/health
      GET  /api/domains
      GET  /api/history
      GET  /api/check?url=<url>&max_pages=<int>&live_crawl=<bool>&archive=<bool>
      POST /api/check  (JSON body: {"url": "...", "max_internal_pages": 5, ...})

Strictly read-only with respect to ./data/linkpilot.db (mode=ro).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, List, Tuple
from urllib.parse import parse_qs, urlsplit

from loguru import logger

from app.authority.ondemand import (
    OnDemandAuthorityAnalyzer,
    OnDemandAuthorityReport,
    normalize_user_target,
    warmup_authority_datasets_async,
)
from app.database.mongo_store import (
    canonicalize_unique_url_key,
    save_bulk_urls_to_mongodb_async,
)
from app.web.pages import get_footer_page_html, is_footer_page

# Trigger background dataset warmup on server module load so first request is instant
warmup_authority_datasets_async("./data/linkpilot.db")


# ==============================================================================
# IN-MEMORY SESSION HISTORY STORE
# ==============================================================================

_HISTORY_LOCK = threading.Lock()
_RECENT_CHECKS: Deque[Dict[str, Any]] = deque(maxlen=25)


def record_check_in_history(report: OnDemandAuthorityReport) -> None:
    """Store a compact summary of a completed check in the server session history."""
    if report.status != "COMPLETED":
        return
    entry = {
        "query_input": report.query_input,
        "target_url": report.target_url,
        "domain_name": report.domain_name,
        "lp_da_display": report.lp_da_display,
        "target_pa_display": report.target_pa_display,
        "scored_pages_count": report.scored_pages_count,
        "total_links_found": report.total_links_found,
        "authority_tier": report.authority_tier,
        "checked_at": report.checked_at,
        "execution_time_ms": report.execution_time_ms,
    }
    with _HISTORY_LOCK:
        # Remove older entry for same target_url if present
        existing = list(_RECENT_CHECKS)
        _RECENT_CHECKS.clear()
        _RECENT_CHECKS.append(entry)
        for item in existing:
            if item["target_url"] != entry["target_url"]:
                _RECENT_CHECKS.append(item)


def get_recent_history() -> List[Dict[str, Any]]:
    """Return list of recent check summaries."""
    with _HISTORY_LOCK:
        return list(_RECENT_CHECKS)


MAX_BULK_URLS = 100
_CSV_HEADER_STOPWORDS = {
    "url", "urls", "domain", "domains", "website", "websites",
    "link", "links", "site", "sites", "page", "pages", "target",
    "address", "host", "hostname", "da", "pa", "spam", "score",
}


def extract_clean_bulk_tokens(raw_items: List[str]) -> List[str]:
    """
    Parse and filter a list of raw URL/domain strings (or comma/newline-delimited blocks),
    returning clean candidate URL/domain tokens.
    """
    tokens: List[str] = []
    for item in raw_items:
        if not item or not isinstance(item, str):
            continue
        for part in re.split(r"[\r\n,;|\t\s]+", item.strip()):
            cleaned = part.strip().strip("\"'`<>()[]{}")
            if not cleaned:
                continue
            low = cleaned.lower()
            if low in _CSV_HEADER_STOPWORDS:
                continue
            # Must look like a URL or domain containing a dot (e.g. example.com)
            if "." not in cleaned or len(cleaned) < 4:
                continue
            tokens.append(cleaned)
    return tokens


def evaluate_bulk_urls(
    raw_urls: List[str],
    db_path: str = "./data/linkpilot.db",
    enable_live_crawl: bool = True,
    enable_archive_lookup: bool = False,
) -> Dict[str, Any]:
    """
    Deduplicate and evaluate a batch of URLs for DA, PA, and Spam Score ONLY,
    and persist unique URLs to MongoDB (skipping duplicates).
    """
    candidate_tokens = extract_clean_bulk_tokens(raw_urls)
    total_submitted = len(candidate_tokens)
    if total_submitted == 0:
        return {
            "status": "FAILED",
            "error_message": "No valid URLs or domains found in input.",
            "total_submitted": 0,
            "unique_evaluated": 0,
            "duplicates_skipped": 0,
            "results": [],
        }

    unique_targets: List[Tuple[str, str, str, str]] = []
    seen_target_urls: set[str] = set()
    duplicates_skipped = 0

    for raw_u in candidate_tokens:
        try:
            target_url, _target_key, root_url, site_boundary = normalize_user_target(raw_u)
            if not site_boundary or "." not in site_boundary:
                continue
            canon_key = canonicalize_unique_url_key(target_url, site_boundary)
            if not canon_key:
                continue
            if canon_key in seen_target_urls:
                duplicates_skipped += 1
                continue
            seen_target_urls.add(canon_key)
            unique_targets.append((raw_u, canon_key, root_url, site_boundary))
        except Exception:
            continue

    if not unique_targets:
        return {
            "status": "FAILED",
            "error_message": "Could not normalize any valid URLs from the provided list.",
            "total_submitted": total_submitted,
            "unique_evaluated": 0,
            "duplicates_skipped": duplicates_skipped,
            "results": [],
        }

    capped_targets = unique_targets[:MAX_BULK_URLS]

    def _eval_single(item: Tuple[str, str, str, str]) -> Dict[str, Any]:
        raw_u, norm_target_url, norm_root_url, site_boundary = item
        analyzer = OnDemandAuthorityAnalyzer(
            db_path=db_path,
            max_internal_pages=1,
            http_timeout=3.0,
            archive_timeout=2.0,
            enable_live_crawl=enable_live_crawl,
            enable_archive_lookup=enable_archive_lookup,
        )
        rep = analyzer.analyze(norm_target_url)
        record_check_in_history(rep)
        return {
            "input_url": raw_u,
            "target_url": rep.target_url or norm_target_url,
            "root_url": rep.root_url or norm_root_url,
            "domain_name": rep.domain_name or site_boundary,
            "status": rep.status,
            "error_message": rep.error_message,
            "lp_da_display": round(float(rep.lp_da_display or 0.0), 1),
            "target_pa_display": round(float(rep.target_pa_display or 0.0), 1),
            "moz_spam_score_pct": round(float(rep.moz_spam_score_pct or 1.0), 1),
            "moz_spam_risk_level": rep.moz_spam_risk_level or "Low",
            "authority_tier": rep.authority_tier or "Standard",
            "checked_at": rep.checked_at,
            "execution_time_ms": round(float(rep.execution_time_ms or 0.0), 1),
        }

    max_workers = min(10, max(1, len(capped_targets)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        evaluated_list = list(pool.map(_eval_single, capped_targets))

    results: List[Dict[str, Any]] = []
    for idx, res in enumerate(evaluated_list):
        res["rank"] = idx + 1
        results.append(res)

    # Persist all unique evaluated URLs to MongoDB asynchronously (upsert=True guarantees zero duplicates)
    save_bulk_urls_to_mongodb_async(results)

    return {
        "status": "COMPLETED",
        "total_submitted": total_submitted,
        "unique_evaluated": len(results),
        "duplicates_skipped": duplicates_skipped,
        "results": results,
    }


def get_known_db_domains(db_path: str = "./data/linkpilot.db") -> List[Dict[str, Any]]:
    """Query known domains from ./data/linkpilot.db in strict read-only mode (mode=ro)."""
    if not db_path or not os.path.exists(db_path):
        return []
    abs_path = os.path.abspath(db_path)
    uri = f"file:{abs_path}?mode=ro"
    conn = None
    domains: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT d.domain_name, COUNT(p.id) as page_count
            FROM domains d
            LEFT JOIN pages p ON p.domain_id = d.id
            GROUP BY d.id
            ORDER BY page_count DESC, d.domain_name ASC
            LIMIT 25
        """)
        for row in cur.fetchall():
            domains.append({
                "domain_name": row["domain_name"],
                "page_count": row["page_count"],
            })
    except Exception as e:
        logger.debug(f"Could not read known domains from {db_path}: {e}")
    finally:
        if conn is not None:
            conn.close()
    return domains


# ==============================================================================
# INTERACTIVE DASHBOARD HTML TEMPLATE
# ==============================================================================

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>RizeMetrics - Unlimited Domain &amp; Page Authority Checker</title>

  <!-- Primary SEO Meta Tags -->
  <meta name="title" content="RizeMetrics - Unlimited Domain &amp; Page Authority Checker">
  <meta name="description" content="Check Domain Authority (DA), Page Authority (PA), and Spam Score instantly. Analyze unlimited URLs with our free DA PA Checker—no sign-up, login, or daily limits.">
  <meta name="keywords" content="DA PA checker, domain authority checker, page authority checker, free DA checker, free PA checker, domain authority tool, page authority tool, spam score checker, website authority checker, backlink checker, bulk DA PA checker, SEO checker">
  <meta name="author" content="RizeMetrics">

  <!-- Canonical URL -->
  <link rel="canonical" href="https://rizemetricstool.com/">

  <!-- Open Graph Meta Tags (Facebook, LinkedIn, etc.) -->
  <meta property="og:type" content="website">
  <meta property="og:title" content="RizeMetrics - Unlimited Domain &amp; Page Authority Checker">
  <meta property="og:description" content="Check Domain Authority (DA), Page Authority (PA), and Spam Score instantly. Analyze unlimited URLs with our free DA PA Checker—no sign-up, login, or daily limits.">
  <meta property="og:url" content="https://rizemetricstool.com/">
  <meta property="og:site_name" content="RizeMetrics">
  <meta property="og:locale" content="en_US">
  <meta property="og:image" content="https://rizemetricstool.com/">
  <meta property="og:image:alt" content="RizeMetrics Free Domain Authority and Page Authority Checker">

  <!-- Twitter Card Meta Tags -->
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="RizeMetrics - Unlimited Domain &amp; Page Authority Checker">
  <meta name="twitter:description" content="Check Domain Authority (DA), Page Authority (PA), and Spam Score instantly. Analyze unlimited URLs with our free DA PA Checker—no sign-up, login, or daily limits.">
  <meta name="twitter:image" content="https://rizemetricstool.com/">
  <meta name="twitter:image:alt" content="RizeMetrics Free DA PA Checker">

  <!-- Optional SEO Metadata -->
  <meta name="application-name" content="RizeMetrics">

  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "Organization",
        "@id": "https://rizemetricstool.com/#organization",
        "name": "RizeMetrics",
        "alternateName": "Rize Metrics",
        "url": "https://rizemetricstool.com/",
        "description": "RizeMetrics is an SEO intelligence and website authority scoring platform.",
        "parentOrganization": {
          "@type": "Organization",
          "@id": "https://rizemetricstool.com/#parent-organization",
          "name": "Rizeworld Digital Marketing Company"
        }
      },
      {
        "@type": "WebSite",
        "@id": "https://rizemetricstool.com/#website",
        "url": "https://rizemetricstool.com/",
        "name": "RizeMetrics",
        "publisher": {
          "@id": "https://rizemetricstool.com/#organization"
        },
        "inLanguage": "en"
      },
      {
        "@type": "WebPage",
        "@id": "https://rizemetricstool.com/#webpage",
        "url": "https://rizemetricstool.com/",
        "name": "Free Domain Authority Checker - RizeMetrics",
        "description": "Check Domain Authority, Page Authority, website Spam Score, ranking keywords and search competitors with RizeMetrics.",
        "isPartOf": {
          "@id": "https://rizemetricstool.com/#website"
        },
        "about": {
          "@id": "https://rizemetricstool.com/#software"
        },
        "publisher": {
          "@id": "https://rizemetricstool.com/#organization"
        },
        "inLanguage": "en"
      },
      {
        "@type": "WebApplication",
        "@id": "https://rizemetricstool.com/#software",
        "name": "RizeMetrics SEO Authority & PageRank Engine",
        "url": "https://rizemetricstool.com/",
        "applicationCategory": "BusinessApplication",
        "operatingSystem": "Any",
        "description": "RizeMetrics is a free web-based SEO intelligence platform for Domain Authority, Page Authority, backlink analysis, website crawling and 27-signal Spam Score auditing.",
        "browserRequirements": "Requires a modern web browser and internet connection.",
        "isAccessibleForFree": true,
        "offers": {
          "@type": "Offer",
          "price": "0",
          "priceCurrency": "USD"
        },
        "featureList": [
          "Domain Authority analysis",
          "Page Authority analysis",
          "LP-DA authority scoring",
          "LP-PA page authority scoring",
          "27-signal Spam Score diagnostics",
          "Backlink analysis",
          "Internal link analysis",
          "External link analysis",
          "Live website mini-crawler",
          "Wayback CDX history",
          "Ranking keyword insights",
          "Search competitor discovery",
          "Bulk URL analysis"
        ],
        "provider": {
          "@id": "https://rizemetricstool.com/#organization"
        },
        "isPartOf": {
          "@id": "https://rizemetricstool.com/#website"
        }
      }
    ]
  }
  </script>

  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "HowTo",
    "@id": "https://rizemetricstool.com/#how-to-use",
    "name": "How to Check Domain Authority and Page Authority with RizeMetrics",
    "description": "Learn how to analyze Domain Authority, Page Authority, backlink data and Spam Risk Score using the free RizeMetrics SEO Authority Checker.",
    "mainEntityOfPage": {
      "@type": "WebPage",
      "@id": "https://rizemetricstool.com/#webpage"
    },
    "step": [
      {
        "@type": "HowToStep",
        "position": 1,
        "name": "Enter a domain or URL",
        "text": "Enter the domain name or specific webpage URL you want to analyze in the RizeMetrics authority checker input field."
      },
      {
        "@type": "HowToStep",
        "position": 2,
        "name": "Select analysis options",
        "text": "Choose the analysis options available in the checker, such as Live Mini-Crawler and Wayback CDX History, according to your needs."
      },
      {
        "@type": "HowToStep",
        "position": 3,
        "name": "Choose the crawl page limit",
        "text": "Select the maximum number of internal pages you want the website mini-crawler to analyze."
      },
      {
        "@type": "HowToStep",
        "position": 4,
        "name": "Analyze website authority",
        "text": "Click the Analyze Authority button to run the website authority analysis."
      },
      {
        "@type": "HowToStep",
        "position": 5,
        "name": "Review authority and spam metrics",
        "text": "Review the Domain Authority, Page Authority and Spam Risk Score metrics, including the available authority and spam diagnostics."
      },
      {
        "@type": "HowToStep",
        "position": 6,
        "name": "Explore backlink and page data",
        "text": "Explore the available result sections to review constituent pages, inbound backlinks, internal links, external outbound links and the 27-Signal Spam Audit."
      }
    ]
  }
  </script>

  <link rel="icon" type="image/png" href="/logo.png" />
  <link rel="shortcut icon" type="image/png" href="/logo.png" />
  <link rel="apple-touch-icon" href="/logo.png" />
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@500;600;700;800&family=Plus+Jakarta+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-canvas: #f1f5f9;
      --bg-card: #ffffff;
      --bg-subtle: #f8fafc;
      --border-light: #cbd5e1;
      --border-subtle: #e2e8f0;
      
      --text-main: #0f172a;
      --text-muted: #334155;
      --text-soft: #64748b;
      --text-light: #94a3b8;
      
      --accent-lime: #d9f942;
      --accent-lime-hover: #c8ea2e;
      --nav-dark: #171b26;
      
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      
      --shadow-card: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -2px rgba(0, 0, 0, 0.03);
      --shadow-hover: 0 12px 20px -4px rgba(0, 0, 0, 0.08), 0 4px 6px -4px rgba(0, 0, 0, 0.04);
      --radius-xl: 20px;
      --radius-2xl: 24px;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }

    html, body {
      max-width: 100%;
      overflow-x: hidden;
      position: relative;
    }

    body {
      background-color: var(--bg-canvas);
      color: var(--text-main);
      min-height: 100vh;
      line-height: 1.5;
      padding-bottom: 0;
    }

    h1, h2, h3, .brand-title, .page-main-title, .gauge-number, .kpi-title {
      font-family: 'Outfit', sans-serif;
    }

    .mono {
      font-family: 'JetBrains Mono', monospace;
    }

    /* Top Floating Capsule Navigation */
    .nav-container {
      width: 96%;
      max-width: 1860px;
      margin: 0 auto;
      padding: 1.25rem 1.5rem 0.6rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
    }

    @media (max-width: 768px) {
      .nav-container {
        width: 100%;
        padding: 0.75rem 1rem 0.5rem;
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 0.75rem;
      }
      .brand-group {
        gap: 0.65rem;
      }
      .brand-logo-img {
        width: 36px;
        height: 36px;
      }
      .brand-title {
        font-size: 1.15rem;
      }
      .brand-subtitle {
        display: none;
      }
      .nav-actions {
        margin-left: auto;
      }
      .quick-export-btn {
        padding: 0.38rem 0.85rem;
        font-size: 0.78rem;
      }
      .capsule-navbar {
        width: 100%;
        order: 3;
        justify-content: center;
        padding: 0.25rem 0.35rem;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        border-radius: 9999px;
      }
      .capsule-item {
        padding: 0.38rem 0.75rem;
        font-size: 0.76rem;
        white-space: nowrap;
      }
    }

    .brand-group {
      display: flex;
      align-items: center;
      gap: 0.85rem;
      text-decoration: none;
    }

    .brand-logo-img {
      width: 44px;
      height: 44px;
      border-radius: 12px;
      background: #ffffff;
      padding: 3px;
      object-fit: contain;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
      border: 1px solid var(--border-light);
    }

    .brand-title {
      font-weight: 800;
      font-size: 1.25rem;
      letter-spacing: -0.02em;
      line-height: 1.15;
    }

    .brand-rize {
      color: #ee591e;
    }

    .brand-metrics {
      color: #2a7c37;
    }

    .brand-subtitle {
      font-size: 0.72rem;
      color: var(--text-soft);
      font-weight: 500;
      display: block;
    }

    .capsule-navbar {
      display: flex;
      align-items: center;
      background: var(--nav-dark);
      border-radius: 9999px;
      padding: 0.35rem 0.45rem;
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.15);
      border: 1px solid #282f3f;
    }

    .capsule-item {
      padding: 0.45rem 1rem;
      font-size: 0.8rem;
      font-weight: 600;
      color: #94a3b8;
      border-radius: 9999px;
      text-decoration: none;
      transition: all 0.15s ease;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.45rem;
      border: none;
      background: transparent;
    }

    .capsule-item:hover {
      color: #ffffff;
    }

    .capsule-item.active {
      background: var(--accent-lime);
      color: #0f172a;
      font-weight: 700;
      box-shadow: 0 2px 8px rgba(217, 249, 66, 0.35);
    }
    /* Hidden Math Proof elements across all views */
    #navBtnMath,
    #btnTabMath,
    #tabMath {
      display: none !important;
    }


    .nav-actions {
      display: flex;
      align-items: center;
      gap: 0.6rem;
    }

    .status-pill {
      display: flex;
      align-items: center;
      gap: 0.45rem;
      background: #ffffff;
      border: 1px solid var(--border-light);
      padding: 0.4rem 0.85rem;
      border-radius: 999px;
      font-size: 0.76rem;
      font-weight: 600;
      color: var(--text-muted);
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
    }

    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #059669;
      box-shadow: 0 0 6px rgba(5, 150, 105, 0.5);
    }

    .container {
      width: 96%;
      max-width: 1860px;
      margin: 0 auto;
      padding: 1.25rem 1.5rem 3rem;
    }

    .page-header-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1.25rem;
    }

    .page-title-wrap {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }

    .back-btn-pill {
      width: 34px;
      height: 34px;
      border-radius: 50%;
      background: #ffffff;
      border: 1px solid var(--border-light);
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--text-muted);
      cursor: pointer;
      transition: all 0.15s;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }

    .back-btn-pill:hover {
      color: var(--text-main);
      border-color: #94a3b8;
    }

    .page-main-title {
      font-size: 1.7rem;
      font-weight: 800;
      letter-spacing: -0.03em;
      color: #0f172a;
    }

    .quick-export-btn {
      padding: 0.55rem 1.15rem;
      font-size: 0.82rem;
      font-weight: 700;
      border-radius: 12px;
      background: #ffffff;
      border: 1px solid var(--border-light);
      color: #0f172a;
      display: flex;
      align-items: center;
      gap: 0.45rem;
      cursor: pointer;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
      transition: all 0.15s;
    }

    .quick-export-btn:hover {
      background: #f8fafc;
      border-color: #94a3b8;
    }

    /* Hero Heading Section Above Search Bar */
    /* =====================================================================
       CURVED HERO SECTION (MOZ-STYLE WITH ACCENT LIME #d9f942)
       ===================================================================== */
    .curved-hero-section {
      width: 100%;
      background: var(--accent-lime);
      background-image: radial-gradient(circle at 50% 20%, rgba(255, 255, 255, 0.42) 0%, rgba(217, 249, 66, 0) 70%),
                        linear-gradient(180deg, #dcfc40 0%, #cbee26 100%);
      padding: 3.5rem 1.5rem 4.5rem;
      position: relative;
      text-align: center;
      border-bottom-left-radius: 50% 65px;
      border-bottom-right-radius: 50% 65px;
      box-shadow: 0 12px 36px rgba(217, 249, 66, 0.32);
      margin-bottom: 2rem;
      overflow: hidden;
      box-sizing: border-box;
    }

    .hero-inner-container {
      max-width: 860px;
      width: 100%;
      margin: 0 auto;
      position: relative;
      z-index: 2;
      padding: 0 0.5rem;
      box-sizing: border-box;
    }

    .hero-main-title {
      font-family: 'Outfit', sans-serif;
      font-size: clamp(1.55rem, 5.2vw, 2.75rem);
      font-weight: 800;
      letter-spacing: -0.03em;
      color: #0f172a;
      line-height: 1.2;
      margin-bottom: 0.85rem;
      word-break: normal;
      overflow-wrap: break-word;
      padding: 0 0.5rem;
    }

    .hero-desc {
      font-size: clamp(0.85rem, 3.2vw, 1.02rem);
      color: #1e293b;
      line-height: 1.55;
      max-width: 720px;
      margin: 0 auto 1.8rem;
      font-weight: 500;
      padding: 0 0.5rem;
    }

    /* Floating Capsule Search Bar Form */
    .capsule-search-form {
      max-width: 780px;
      width: 100%;
      margin: 0 auto 0.85rem;
      box-sizing: border-box;
    }

    .capsule-search-pill {
      display: flex;
      align-items: center;
      background: #ffffff;
      border-radius: 9999px;
      padding: 0.45rem 0.55rem 0.45rem 1.45rem;
      box-shadow: 0 14px 40px rgba(15, 23, 42, 0.12), 0 2px 8px rgba(0, 0, 0, 0.04);
      border: 2.5px solid rgba(255, 255, 255, 0.95);
      transition: all 0.25s ease;
      width: 100%;
      box-sizing: border-box;
    }

    .capsule-search-pill:focus-within {
      border-color: #0f172a;
      box-shadow: 0 18px 48px rgba(15, 23, 42, 0.2), 0 0 0 4px rgba(15, 23, 42, 0.08);
      transform: translateY(-2px);
    }

    .capsule-input-icon {
      display: flex;
      align-items: center;
      justify-content: center;
      color: #94a3b8;
      margin-right: 0.65rem;
      flex-shrink: 0;
    }

    .capsule-url-input {
      flex: 1;
      border: none;
      outline: none;
      background: transparent;
      font-size: 1.02rem;
      font-weight: 500;
      color: #0f172a;
      font-family: inherit;
      min-width: 0;
    }

    .capsule-url-input::placeholder {
      color: #94a3b8;
      font-weight: 400;
    }

    .capsule-check-btn {
      background: #0f172a;
      color: #ffffff;
      border: none;
      border-radius: 9999px;
      padding: 0.85rem 1.95rem;
      font-size: 0.92rem;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.55rem;
      white-space: nowrap;
      transition: all 0.2s ease;
      box-shadow: 0 4px 14px rgba(15, 23, 42, 0.28);
      flex-shrink: 0;
    }

    .capsule-check-btn:hover:not(:disabled) {
      background: #1e293b;
      transform: scale(1.02);
      box-shadow: 0 6px 20px rgba(15, 23, 42, 0.38);
    }

    .capsule-check-btn:disabled {
      opacity: 0.65;
      cursor: not-allowed;
    }

    .capsule-csv-btn {
      background: #f1f5f9;
      color: #0f172a;
      border: 1.5px solid #cbd5e1;
      border-radius: 9999px;
      padding: 0.78rem 1.25rem;
      font-size: 0.86rem;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.45rem;
      white-space: nowrap;
      transition: all 0.2s ease;
      margin-right: 0.45rem;
      flex-shrink: 0;
    }

    .capsule-csv-btn:hover:not(:disabled) {
      background: #e2e8f0;
      border-color: #94a3b8;
      transform: translateY(-1px);
    }

    .capsule-csv-btn:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }

    @media (max-width: 640px) {
      .capsule-search-pill {
        flex-wrap: wrap;
        border-radius: 22px;
        padding: 0.65rem 0.75rem;
        gap: 0.5rem;
      }
      .capsule-url-input {
        width: 100%;
        flex: 1 1 80%;
        padding: 0.35rem 0;
      }
      .capsule-csv-btn,
      .capsule-check-btn {
        flex: 1 1 calc(50% - 0.35rem);
        justify-content: center;
        margin-right: 0;
        padding: 0.72rem 0.85rem;
        font-size: 0.83rem;
      }
    }

    /* Bulk URL Results Table Section */
    .bulk-results-card {
      background: #ffffff;
      border-radius: 24px;
      border: 1px solid var(--border-light);
      box-shadow: 0 12px 34px rgba(15, 23, 42, 0.06);
      padding: 1.65rem 1.85rem;
      margin-bottom: 2.25rem;
    }

    .bulk-header-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 1rem;
      margin-bottom: 1.25rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid #e2e8f0;
    }

    .bulk-title-group h2 {
      font-family: 'Outfit', sans-serif;
      font-size: 1.38rem;
      font-weight: 800;
      color: #0f172a;
      margin: 0 0 0.25rem 0;
      display: flex;
      align-items: center;
      gap: 0.55rem;
    }

    .bulk-title-group p {
      font-size: 0.86rem;
      color: #64748b;
      margin: 0;
    }

    .bulk-actions-group {
      display: flex;
      align-items: center;
      gap: 0.65rem;
      flex-wrap: wrap;
    }

    .bulk-export-btn {
      background: #0f172a;
      color: #ffffff;
      border: none;
      border-radius: 12px;
      padding: 0.62rem 1.15rem;
      font-size: 0.84rem;
      font-weight: 700;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      transition: all 0.18s ease;
    }

    .bulk-export-btn:hover {
      background: #1e293b;
      transform: translateY(-1px);
    }

    .bulk-clear-btn {
      background: #f8fafc;
      color: #475569;
      border: 1px solid #cbd5e1;
      border-radius: 12px;
      padding: 0.62rem 1rem;
      font-size: 0.84rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.18s ease;
    }

    .bulk-clear-btn:hover {
      background: #f1f5f9;
      color: #0f172a;
    }

    .bulk-summary-strip {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 0.85rem;
      margin-bottom: 1.35rem;
    }

    .bulk-stat-chip {
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 16px;
      padding: 0.85rem 1.1rem;
      display: flex;
      flex-direction: column;
      gap: 0.2rem;
    }

    .bulk-stat-label {
      font-size: 0.74rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: #64748b;
    }

    .bulk-stat-val {
      font-family: 'Outfit', sans-serif;
      font-size: 1.35rem;
      font-weight: 800;
      color: #0f172a;
    }

    .bulk-table-wrap {
      width: 100%;
      overflow-x: auto;
      border-radius: 16px;
      border: 1px solid #e2e8f0;
    }

    .bulk-table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 0.9rem;
    }

    .bulk-table th {
      background: #f8fafc;
      color: #334155;
      font-weight: 700;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      padding: 0.9rem 1.1rem;
      border-bottom: 1px solid #e2e8f0;
      white-space: nowrap;
    }

    .bulk-table td {
      padding: 0.9rem 1.1rem;
      border-bottom: 1px solid #f1f5f9;
      vertical-align: middle;
    }

    .bulk-table tbody tr:last-child td {
      border-bottom: none;
    }

    .bulk-table tbody tr:hover {
      background: #f8fafc;
    }

    .bulk-metric-pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0.35rem 0.8rem;
      border-radius: 999px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.86rem;
      font-weight: 700;
      min-width: 64px;
    }

    .bulk-pill-da {
      background: #ffedd5;
      color: #c2410c;
      border: 1px solid #fed7aa;
    }

    .bulk-pill-pa {
      background: #dbeafe;
      color: #1d4ed8;
      border: 1px solid #bfdbfe;
    }

    .bulk-pill-spam-low {
      background: #dcfce7;
      color: #15803d;
      border: 1px solid #bbf7d0;
    }

    .bulk-pill-spam-med {
      background: #fef3c7;
      color: #b45309;
      border: 1px solid #fde68a;
    }

    .bulk-pill-spam-high {
      background: #fee2e2;
      color: #b91c1c;
      border: 1px solid #fecaca;
    }

    /* Crawler options row underneath capsule */
    .hero-options-row {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.75rem;
      flex-wrap: wrap;
      margin-top: 1.25rem;
      width: 100%;
      box-sizing: border-box;
      padding: 0 0.25rem;
    }

    .hero-opt-badge {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      background: rgba(255, 255, 255, 0.65);
      backdrop-filter: blur(8px);
      border: 1px solid rgba(15, 23, 42, 0.08);
      padding: 0.38rem 0.95rem;
      border-radius: 9999px;
      font-size: 0.82rem;
      font-weight: 600;
      color: #0f172a;
      cursor: pointer;
      transition: all 0.15s ease;
    }

    .hero-opt-badge:hover {
      background: rgba(255, 255, 255, 0.9);
      border-color: rgba(15, 23, 42, 0.2);
    }

    .hero-select {
      background: transparent;
      border: none;
      color: #0f172a;
      font-weight: 700;
      font-size: 0.82rem;
      outline: none;
      cursor: pointer;
    }

    /* Fallback / legacy aliases */
    .filter-card { display: none; }
    .search-bar-row { display: flex; }
    .url-input { border: none; }
    .check-btn { composes: capsule-check-btn; }

    @media (max-width: 640px) {
      .curved-hero-section {
        padding: 2.2rem 0.85rem 3.4rem;
        border-bottom-left-radius: 50% 32px;
        border-bottom-right-radius: 50% 32px;
        margin-bottom: 1.5rem;
      }
      .capsule-search-pill {
        flex-direction: column;
        border-radius: 20px;
        padding: 0.75rem;
        gap: 0.65rem;
        align-items: stretch;
      }
      .capsule-input-icon {
        display: none;
      }
      .capsule-url-input {
        width: 100%;
        padding: 0.35rem 0.25rem;
        font-size: 0.95rem;
        text-align: center;
      }
      .capsule-check-btn {
        width: 100%;
        justify-content: center;
        padding: 0.75rem 1.2rem;
        font-size: 0.88rem;
      }
      .hero-options-row {
        gap: 0.45rem;
        margin-top: 1rem;
        padding: 0 0.25rem;
      }
      .hero-opt-badge {
        font-size: 0.74rem;
        padding: 0.3rem 0.7rem;
      }
    }

    .controls-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.85rem;
      padding-top: 0.75rem;
      border-top: 1px solid var(--border-subtle);
    }

    .options-group {
      display: flex;
      align-items: center;
      gap: 1.25rem;
      flex-wrap: wrap;
      font-size: 0.8rem;
      color: var(--text-muted);
    }

    .options-group label {
      display: flex;
      align-items: center;
      gap: 0.4rem;
      cursor: pointer;
      font-weight: 600;
    }

    .options-group select {
      background: #f8fafc;
      color: var(--text-main);
      border: 1px solid var(--border-light);
      border-radius: 8px;
      padding: 0.25rem 0.5rem;
      font-size: 0.78rem;
      font-weight: 600;
      outline: none;
    }

    .preset-pills {
      display: flex;
      align-items: center;
      gap: 0.4rem;
      flex-wrap: wrap;
    }

    .preset-label {
      font-size: 0.75rem;
      color: var(--text-soft);
      font-weight: 600;
      margin-right: 0.1rem;
    }

    .preset-btn {
      font-size: 0.75rem;
      padding: 0.28rem 0.65rem;
      border-radius: 8px;
      background: #f8fafc;
      border: 1px solid var(--border-light);
      color: var(--text-main);
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s;
    }

    .preset-btn:hover {
      background: #eff6ff;
      border-color: #bfdbfe;
      color: var(--primary);
    }

    .loading-banner {
      display: none;
      background: #ffffff;
      border: 1.5px solid #bfdbfe;
      border-radius: var(--radius-xl);
      padding: 1.25rem 1.5rem;
      margin-bottom: 1.35rem;
      align-items: center;
      gap: 1.15rem;
      box-shadow: 0 4px 12px rgba(37, 99, 235, 0.08);
    }

    .loading-banner.active {
      display: flex;
    }

    .spinner {
      width: 28px;
      height: 28px;
      border: 3px solid rgba(37, 99, 235, 0.18);
      border-top-color: var(--primary);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      flex-shrink: 0;
    }

    @keyframes spin {
      to { transform: rotate(360deg); }
    }

    .error-banner {
      display: none;
      background: #fef2f2;
      border: 1px solid #fecaca;
      color: #dc2626;
      padding: 1rem 1.25rem;
      border-radius: var(--radius-xl);
      margin-bottom: 1.35rem;
      font-size: 0.85rem;
      font-weight: 600;
    }

    /* =========================================================================
       TOP 4 BENTO KPI CARDS - FULL PASTEL BACKGROUNDS (Exactly as in user's image)
       Large rounded-3xl corners, full card pastel color, white contrasting pills
       ========================================================================= */
    .top-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 1.4rem;
      margin-bottom: 1.4rem;
      width: 100%;
    }

    @media (max-width: 992px) {
      .top-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 1.25rem;
      }
    }

    @media (max-width: 640px) {
      .top-grid {
        grid-template-columns: 1fr;
        gap: 1.15rem;
      }
      .kpi-card {
        border-radius: 20px;
        padding: 1.35rem 1.15rem;
        min-height: auto;
      }
      .kpi-header {
        flex-direction: column;
        align-items: flex-start;
        gap: 0.65rem;
      }
      .kpi-title-row {
        width: 100%;
      }
      .kpi-title {
        font-size: 0.92rem;
        white-space: normal;
      }
      .tier-badge {
        align-self: flex-start;
        font-size: 0.72rem;
        padding: 0.25rem 0.7rem;
      }
      .gauge-visual-wrap {
        flex-direction: column;
        align-items: center;
        gap: 1.15rem;
        margin: 0.85rem 0 1.15rem;
      }
      .radial-gauge {
        margin: 0 auto;
      }
      .sub-metrics {
        width: 100%;
        gap: 0.65rem;
      }
      .sub-metric-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        width: 100%;
        gap: 0.75rem;
      }
      .sub-metric-label {
        max-width: none;
        white-space: normal;
        font-size: 0.8rem;
      }
      .sub-metric-val {
        font-size: 0.84rem;
        white-space: nowrap;
        flex-shrink: 0;
      }
      .stat-grid-2x2 {
        gap: 0.65rem;
      }
      .stat-box {
        padding: 0.8rem 0.9rem;
      }
      .stat-box-value {
        font-size: 1.4rem;
      }
      .stat-box-label {
        font-size: 0.72rem;
      }
      .card-footer-meta {
        flex-wrap: wrap;
        gap: 0.5rem;
        font-size: 0.75rem;
        padding-top: 0.75rem;
      }
    }

    .kpi-card {
      border-radius: 26px;
      padding: 1.75rem 1.8rem;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      transition: transform 0.2s, box-shadow 0.2s;
      position: relative;
      min-height: 330px;
      min-width: 0;
      overflow: hidden;
    }

    .kpi-card:hover {
      box-shadow: var(--shadow-hover);
      transform: translateY(-3px);
    }

    /* CARD 1: Full Pastel Peach / Orange */
    .kpi-card.card-da {
      background-color: #ffedd5;
      border: 1px solid #fed7aa;
    }
    .kpi-card.card-da .kpi-title {
      color: #9a3412;
    }
    .kpi-card.card-da .kpi-subtitle {
      color: #c2410c;
    }
    .kpi-card.card-da .card-footer-meta {
      border-color: rgba(194, 65, 12, 0.18);
      color: #9a3412;
    }

    /* CARD 2: Full Pastel Sky / Baby Blue */
    .kpi-card.card-pa {
      background-color: #dbeafe;
      border: 1px solid #bfdbfe;
    }
    .kpi-card.card-pa .kpi-title {
      color: #1e40af;
    }
    .kpi-card.card-pa .kpi-subtitle {
      color: #2563eb;
    }
    .kpi-card.card-pa .card-footer-meta {
      border-color: rgba(30, 64, 175, 0.18);
      color: #1e40af;
    }

    /* CARD 3: Full Pastel Fresh Mint / Green */
    .kpi-card.card-spam {
      background-color: #d1fae5;
      border: 1px solid #a7f3d0;
    }
    .kpi-card.card-spam .kpi-title {
      color: #065f46;
    }
    .kpi-card.card-spam .kpi-subtitle {
      color: #059669;
    }
    .kpi-card.card-spam .card-footer-meta {
      border-color: rgba(6, 95, 70, 0.18);
      color: #065f46;
    }

    /* CARD 4: Full Pastel Lilac / Lavender (Exact color from user's screenshot!) */
    .kpi-card.card-links {
      background-color: #ede9fe;
      border: 1px solid #ddd6fe;
    }
    .kpi-card.card-links .kpi-title {
      color: #5b21b6;
    }
    .kpi-card.card-links .kpi-subtitle {
      color: #7c3aed;
    }
    .kpi-card.card-links .card-footer-meta {
      border-color: rgba(91, 33, 182, 0.18);
      color: #5b21b6;
    }

    /* CARD 4: Wide Compact Banner spanning full width */
    .card-telemetry-wide {
      min-height: auto !important;
      padding: 1.25rem 1.6rem !important;
      margin-bottom: 1.4rem !important;
      display: flex !important;
      flex-direction: column !important;
      gap: 0.95rem !important;
      width: 100% !important;
      box-sizing: border-box !important;
      border-radius: 24px !important;
    }

    .telemetry-row-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
      flex-wrap: wrap;
      width: 100%;
    }

    .telemetry-meta-right {
      display: flex;
      align-items: center;
      gap: 1.25rem;
      font-size: 0.8rem;
      font-weight: 700;
      color: #5b21b6;
      flex-wrap: wrap;
    }

    .telemetry-row-bottom {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1.4rem;
      flex-wrap: wrap;
      width: 100%;
    }

    .telemetry-stats-group {
      display: flex;
      gap: 0.85rem;
      flex: 1 1 540px;
      min-width: 0;
    }

    .telemetry-stats-group .stat-box {
      flex: 1;
      min-width: 85px;
      padding: 0.65rem 0.85rem;
      background: #ffffff;
      border-radius: 14px;
      text-align: center;
      border: 1px solid rgba(124, 58, 237, 0.12);
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.04);
      min-height: auto;
    }

    .telemetry-stats-group .stat-box-value {
      font-size: 1.45rem;
      font-weight: 800;
      color: #0f172a;
      line-height: 1.15;
      font-variant-numeric: tabular-nums;
      font-family: 'Outfit', sans-serif;
    }

    .telemetry-stats-group .stat-box-label {
      font-size: 0.72rem;
      font-weight: 700;
      color: var(--text-soft);
      text-transform: uppercase;
      letter-spacing: 0.03em;
      margin-top: 0.2rem;
    }

    .telemetry-dofollow-group {
      flex: 0 1 340px;
      min-width: 240px;
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
      background: rgba(255, 255, 255, 0.75);
      padding: 0.7rem 1.1rem;
      border-radius: 14px;
      border: 1px solid rgba(124, 58, 237, 0.12);
      box-sizing: border-box;
    }

    @media (max-width: 992px) {
      .telemetry-row-bottom {
        flex-direction: column;
        align-items: stretch;
        gap: 0.85rem;
      }
      .telemetry-stats-group {
        flex: none;
        width: 100%;
      }
      .telemetry-dofollow-group {
        flex: none;
        width: 100%;
      }
    }

    @media (max-width: 640px) {
      .card-telemetry-wide {
        padding: 1.1rem 1.15rem !important;
        border-radius: 20px !important;
      }
      .telemetry-stats-group {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0.6rem;
      }
      .telemetry-meta-right {
        gap: 0.65rem;
        font-size: 0.75rem;
      }
    }

    .kpi-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 0.65rem;
      margin-bottom: 0.85rem;
      flex-wrap: wrap;
    }

    .kpi-title-row {
      display: flex;
      align-items: center;
      gap: 0.65rem;
      min-width: 0;
      flex: 1 1 170px;
    }

    .kpi-title-text {
      min-width: 0;
      flex: 1;
      overflow: visible;
    }

    /* Contrasting Pure White Icon Pills */
    .kpi-icon-pill {
      width: 42px;
      height: 42px;
      border-radius: 14px;
      background: #ffffff;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.06);
      flex-shrink: 0;
    }

    .card-da .kpi-icon-pill { color: #ea580c; }
    .card-pa .kpi-icon-pill { color: #2563eb; }
    .card-spam .kpi-icon-pill { color: #059669; }
    .card-links .kpi-icon-pill { color: #7c3aed; }

    .kpi-title {
      font-size: 0.92rem;
      text-transform: uppercase;
      letter-spacing: 0.03em;
      font-weight: 800;
      white-space: nowrap;
      line-height: 1.2;
    }

    .kpi-subtitle {
      font-size: 0.76rem;
      margin-top: 0.2rem;
      font-weight: 600;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 220px;
    }

    /* Pure White Contrasting Pill Badges (Like the "Keep watching" pill in user's image) */
    .tier-badge {
      font-size: 0.72rem;
      font-weight: 700;
      padding: 0.3rem 0.75rem;
      border-radius: 9999px;
      background: #ffffff;
      white-space: nowrap;
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.06);
      flex-shrink: 0;
      margin-left: auto;
    }

    .card-da .tier-badge { color: #c2410c; }
    .card-pa .tier-badge { color: #1d4ed8; }
    .card-spam .tier-badge { color: #047857; }
    .card-links .tier-badge { color: #6d28d9; }

    /* Gauge Section inside Bento Cards */
    .gauge-visual-wrap {
      display: flex;
      align-items: center;
      gap: 1.15rem;
      margin: 0.85rem 0 1.15rem;
    }

    .radial-gauge {
      position: relative;
      width: 110px;
      height: 110px;
      flex-shrink: 0;
    }

    .radial-gauge svg {
      width: 110px;
      height: 110px;
      transform: rotate(-90deg);
    }

    /* Clean white radial track against pastel background */
    .gauge-bg {
      fill: none;
      stroke: rgba(255, 255, 255, 0.75);
      stroke-width: 8;
    }

    .gauge-fill {
      fill: none;
      stroke-width: 8;
      stroke-linecap: round;
      transition: stroke-dashoffset 0.8s cubic-bezier(0.22, 1, 0.36, 1);
    }

    .gauge-center-text {
      position: absolute;
      inset: 0;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 0 6px;
      box-sizing: border-box;
      pointer-events: none;
    }

    .gauge-number {
      font-family: 'Outfit', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
      font-size: 1.65rem;
      font-weight: 800;
      letter-spacing: -0.04em;
      line-height: 1;
      color: #0f172a;
      white-space: nowrap;
      text-align: center;
      display: block;
      max-width: 90px;
      margin: 0 auto;
      transition: font-size 0.2s ease;
    }

    .gauge-scale {
      font-size: 0.65rem;
      color: rgba(15, 23, 42, 0.7);
      font-weight: 700;
      margin-top: 3px;
      text-transform: uppercase;
      letter-spacing: 0.03em;
      white-space: nowrap;
    }

    .sub-metrics {
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 0.65rem;
      min-width: 0;
    }

    .sub-metric-item {
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
      width: 100%;
    }

    .sub-metric-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.78rem;
      gap: 0.5rem;
      width: 100%;
    }

    .sub-metric-label {
      color: rgba(15, 23, 42, 0.82);
      font-weight: 600;
      font-size: 0.78rem;
      white-space: normal;
      word-break: normal;
      line-height: 1.25;
      flex: 1;
    }

    .sub-metric-val {
      font-weight: 800;
      color: #0f172a;
      font-size: 0.82rem;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      text-align: right;
      flex-shrink: 0;
    }

    .mini-bar-bg {
      width: 100%;
      height: 7px;
      background: rgba(255, 255, 255, 0.7);
      border-radius: 99px;
      overflow: hidden;
    }

    .mini-bar-fill {
      height: 100%;
      border-radius: 99px;
      transition: width 0.6s ease;
    }

    .card-footer-meta {
      padding-top: 1rem;
      border-top: 1px solid;
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.78rem;
      font-weight: 700;
      gap: 0.5rem;
      width: 100%;
      min-width: 0;
      flex-wrap: wrap;
    }

    /* White rounded stat boxes inside Card 4 */
    .stat-grid-2x2 {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0.85rem;
      margin: 0.8rem 0 1.1rem;
      width: 100%;
    }

    .stat-box {
      background: #ffffff;
      border-radius: 16px;
      padding: 0.95rem 1.1rem;
      box-shadow: 0 3px 8px rgba(0, 0, 0, 0.05);
      border: 1px solid rgba(124, 58, 237, 0.12);
      min-width: 0;
      overflow: hidden;
    }

    .stat-box-value {
      font-size: 1.6rem;
      font-weight: 800;
      color: #0f172a;
      font-variant-numeric: tabular-nums;
      font-family: 'Outfit', sans-serif;
      line-height: 1.15;
    }

    .stat-box-label {
      font-size: 0.78rem;
      color: var(--text-soft);
      font-weight: 600;
      margin-top: 0.2rem;
    }

    .white-pill-btn {
      font-size: 0.75rem;
      padding: 0.35rem 0.85rem;
      border-radius: 9999px;
      background: #ffffff;
      color: #0f172a;
      font-weight: 700;
      cursor: pointer;
      border: none;
      box-shadow: 0 2px 4px rgba(0, 0, 0, 0.06);
      transition: all 0.15s;
    }

    .white-pill-btn:hover {
      background: #f8fafc;
      transform: translateY(-1px);
    }

    /* Target Metadata Strip */
    .meta-strip {
      background: #ffffff;
      border: 1px solid var(--border-light);
      border-radius: var(--radius-xl);
      padding: 1.1rem 1.4rem;
      margin-bottom: 1.5rem;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 1.25rem;
      box-shadow: var(--shadow-card);
      box-sizing: border-box;
      width: 100%;
      overflow: hidden;
    }
    .meta-strip > div {
      min-width: 0;
      display: flex;
      flex-direction: column;
      justify-content: flex-start;
    }

    @media (max-width: 900px) {
      .meta-strip {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 1rem;
      }
    }

    @media (max-width: 640px) {
      main.container {
        width: 100%;
        padding: 0 0.85rem 3rem;
        box-sizing: border-box;
        overflow: hidden;
      }
      .meta-strip {
        grid-template-columns: minmax(0, 1fr);
        gap: 0.85rem;
        padding: 1rem 1.15rem;
        border-radius: 16px;
        box-sizing: border-box;
      }
      .meta-strip > div {
        min-width: 0;
      }
      .meta-item-label {
        font-size: 0.65rem;
      }
      .meta-item-value {
        font-size: 0.82rem;
      }
      .clean-explorer-card {
        border-radius: 18px;
      }
      .tabs-header {
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        white-space: nowrap;
        scrollbar-width: none;
        padding: 0.4rem 0.5rem 0;
        gap: 0.35rem;
      }
      .tabs-header::-webkit-scrollbar {
        display: none;
      }
      .tab-btn {
        padding: 0.65rem 0.85rem;
        font-size: 0.78rem;
        flex-shrink: 0;
      }
      .tab-panel {
        padding: 1rem 0.75rem;
      }
      .table-wrap {
        width: 100%;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
      }
      table.data-table {
        min-width: 580px;
        font-size: 0.76rem;
      }
      table.data-table th, table.data-table td {
        padding: 0.65rem 0.6rem;
      }
    }

    .meta-item-label {
      font-size: 0.68rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-soft);
      font-weight: 700;
    }

    .meta-item-value {
      font-size: 0.88rem;
      font-weight: 600;
      color: #0f172a;
      margin-top: 0.15rem;
      word-break: break-word;
      overflow-wrap: anywhere;
      white-space: normal;
      line-height: 1.4;
    }

    /* =========================================================================
       CLEAN LIGHT TABLE EXPLORER (The clean bottom section)
       ========================================================================= */
    .clean-explorer-card {
      background: #ffffff;
      border: 1px solid var(--border-light);
      border-radius: var(--radius-2xl);
      overflow: hidden;
      box-shadow: var(--shadow-card);
    }

    .tabs-header {
      display: flex;
      align-items: center;
      background: #ffffff;
      border-bottom: 1px solid var(--border-subtle);
      overflow-x: auto;
      padding: 0.5rem 1rem 0;
      gap: 0.5rem;
    }

    .tab-btn {
      padding: 0.8rem 1.25rem;
      background: transparent;
      border: none;
      border-bottom: 2.5px solid transparent;
      color: var(--text-muted);
      font-weight: 600;
      font-size: 0.84rem;
      cursor: pointer;
      white-space: nowrap;
      transition: all 0.15s ease;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    .tab-btn:hover {
      color: var(--text-main);
    }

    .tab-btn.active {
      color: var(--primary);
      border-bottom-color: var(--primary);
      font-weight: 700;
    }

    .tab-badge {
      font-size: 0.7rem;
      padding: 0.12rem 0.45rem;
      border-radius: 999px;
      background: #f1f5f9;
      color: var(--text-muted);
      font-weight: 600;
      border: 1px solid var(--border-subtle);
    }

    .tab-btn.active .tab-badge {
      background: #eff6ff;
      color: var(--primary);
      border-color: #bfdbfe;
    }

    .tab-panel {
      display: none;
      padding: 1.5rem;
      scroll-margin-top: 75px;
    }

    .tab-panel.active {
      display: block;
    }

    /* Data Tables in Clean Light Mode */
    .table-wrap {
      overflow-x: auto;
    }

    table.data-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.82rem;
    }

    table.data-table th {
      text-align: left;
      padding: 0.8rem 0.95rem;
      background: #f8fafc;
      color: var(--text-muted);
      font-weight: 700;
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      border-bottom: 1px solid var(--border-subtle);
    }

    table.data-table td {
      padding: 0.85rem 0.95rem;
      border-bottom: 1px solid #f1f5f9;
      vertical-align: middle;
      color: var(--text-muted);
    }

    table.data-table tr:hover td {
      background: #f8fafc;
    }

    table.data-table tr.target-highlight td {
      background: #eff6ff;
    }

    .url-cell {
      max-width: 650px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .url-link {
      color: var(--primary);
      text-decoration: none;
      font-weight: 600;
    }

    .url-link:hover {
      text-decoration: underline;
    }

    .pa-bar-wrap {
      display: flex;
      align-items: center;
      gap: 0.6rem;
      min-width: 130px;
    }

    .pa-bar-bg {
      flex: 1;
      height: 6px;
      background: #e2e8f0;
      border-radius: 99px;
      overflow: hidden;
    }

    .pa-bar-fill {
      height: 100%;
      background: var(--primary);
      border-radius: 99px;
    }

    /* Tag Pills inside Table */
    .tag {
      display: inline-block;
      padding: 0.2rem 0.55rem;
      border-radius: 6px;
      font-size: 0.7rem;
      font-weight: 600;
    }

    .tag-dofollow {
      background: #ecfdf5;
      color: #059669;
      border: 1px solid #a7f3d0;
    }

    .tag-nofollow {
      background: #fffbeb;
      color: #d97706;
      border: 1px solid #fde68a;
    }

    .tag-ctx {
      background: #f1f5f9;
      color: #475569;
      border: 1px solid #e2e8f0;
    }

    .tag-target {
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      color: var(--primary);
      margin-left: 0.4rem;
    }

    .formula-box {
      background: #f8fafc;
      border: 1px solid var(--border-subtle);
      border-radius: 14px;
      padding: 1.35rem;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.84rem;
      color: var(--text-main);
      line-height: 1.7;
    }

    /* Recent History Strip */
    .history-section {
      margin-top: 1.5rem;
    }

    .history-title {
      font-size: 0.78rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      margin-bottom: 0.65rem;
    }

    .history-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 0.85rem;
    }

    .history-item {
      background: #ffffff;
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      padding: 0.85rem 1rem;
      cursor: pointer;
      transition: all 0.15s;
      display: flex;
      justify-content: space-between;
      align-items: center;
      box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }

    .history-item:hover {
      border-color: var(--primary);
      box-shadow: var(--shadow-hover);
      transform: translateY(-1px);
    }

    /* Authority Overview Section Between Hero and Footer */
    .authority-overview-section {
      margin: 2.75rem 0 3.5rem;
      width: 100%;
    }

    .authority-overview-card {
      background: var(--bg-card);
      border-radius: var(--radius-2xl);
      border: 1px solid var(--border-subtle);
      padding: 3.5rem 2.75rem;
      box-shadow: var(--shadow-card);
      text-align: center;
      position: relative;
      overflow: hidden;
      background-image: radial-gradient(circle at 50% 0%, rgba(217, 249, 66, 0.12) 0%, rgba(255, 255, 255, 0) 70%);
    }

    .authority-overview-badge {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      background: var(--nav-dark);
      color: #ffffff;
      font-size: 0.72rem;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      padding: 0.35rem 0.95rem;
      border-radius: 9999px;
      margin-bottom: 1.25rem;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
    }

    .authority-overview-title {
      font-family: 'Outfit', sans-serif;
      font-size: clamp(1.6rem, 3.2vw, 2.35rem);
      font-weight: 800;
      color: var(--text-main);
      line-height: 1.25;
      margin-bottom: 1.2rem;
      letter-spacing: -0.025em;
      max-width: 960px;
      margin-left: auto;
      margin-right: auto;
    }

    .authority-overview-desc {
      font-size: clamp(1rem, 1.8vw, 1.12rem);
      color: var(--text-muted);
      line-height: 1.75;
      max-width: 860px;
      margin: 0 auto 2.5rem;
      font-weight: 500;
    }

    .authority-feature-highlights {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 1.5rem;
      text-align: left;
      margin-top: 1.5rem;
    }

    .authority-highlight-box {
      background: var(--bg-subtle);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-xl);
      padding: 1.5rem 1.4rem;
      display: flex;
      align-items: flex-start;
      gap: 1rem;
      transition: transform 0.2s ease, box-shadow 0.2s ease;
    }

    .authority-highlight-box:hover {
      transform: translateY(-2px);
      box-shadow: var(--shadow-hover);
      border-color: var(--border-light);
    }

    .highlight-icon-wrap {
      width: 44px;
      height: 44px;
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }

    .highlight-title {
      font-family: 'Outfit', sans-serif;
      font-size: 1.02rem;
      font-weight: 700;
      color: var(--text-main);
      margin-bottom: 0.35rem;
      line-height: 1.3;
    }

    .highlight-sub {
      font-size: 0.85rem;
      color: var(--text-soft);
      line-height: 1.55;
      margin: 0;
    }
    /* =========================================================================
     BULLETPROOF CONSOLIDATED RESPONSIVE OVERRIDE (MOBILE & TABLET)
     Placed at the absolute end of <style> so it overrides all base rules cleanly
     ========================================================================= */

  /* Global overflow prevention */
  html, body {
    max-width: 100vw !important;
    overflow-x: hidden !important;
    position: relative !important;
    box-sizing: border-box !important;
  }
  *, *::before, *::after {
    box-sizing: border-box !important;
  }

  /* Tablets & Medium Screens (<= 992px) */
  @media (max-width: 992px) {
    .top-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
      gap: 1.25rem !important;
    }
  }

  /* Mobile Landscape & Tablets (<= 768px) */
  @media (max-width: 768px) {
    .nav-container {
      width: 100% !important;
      max-width: 100% !important;
      padding: 0.65rem 0.85rem 0.45rem !important;
      display: flex !important;
      flex-wrap: wrap !important;
      align-items: center !important;
      justify-content: space-between !important;
      gap: 0.5rem !important;
      box-sizing: border-box !important;
    }
    .brand-group {
      display: flex !important;
      align-items: center !important;
      gap: 0.55rem !important;
      flex-shrink: 0 !important;
    }
    .brand-logo-img {
      width: 34px !important;
      height: 34px !important;
    }
    .brand-title {
      font-size: 1.15rem !important;
      font-weight: 800 !important;
    }
    .brand-subtitle {
      display: none !important;
    }
    .nav-actions {
      margin-left: auto !important;
      flex-shrink: 0 !important;
    }
    .quick-export-btn {
      padding: 0.35rem 0.75rem !important;
      font-size: 0.76rem !important;
    }
    .capsule-navbar {
      width: 100% !important;
      max-width: 100% !important;
      order: 3 !important;
      display: flex !important;
      justify-content: space-around !important;
      align-items: center !important;
      padding: 0.22rem 0.3rem !important;
      box-sizing: border-box !important;
      border-radius: 9999px !important;
      background: var(--nav-dark) !important;
      margin-top: 0.2rem !important;
    }
    .capsule-item:not(#navBtnMath) {
      padding: 0.35rem 0.65rem !important;
      font-size: 0.74rem !important;
      white-space: nowrap !important;
      flex: 1 1 auto !important;
      text-align: center !important;
      display: flex !important;
      align-items: center !important;
      justify-content: center !important;
      gap: 0.3rem !important;
    }

    .curved-hero-section {
      width: 100% !important;
      max-width: 100% !important;
      padding: 2.2rem 0.85rem 3.4rem !important;
      border-bottom-left-radius: 50% 32px !important;
      border-bottom-right-radius: 50% 32px !important;
      margin-bottom: 1.5rem !important;
      box-sizing: border-box !important;
      overflow: hidden !important;
    }
    .hero-inner-container {
      width: 100% !important;
      max-width: 100% !important;
      padding: 0 !important;
      box-sizing: border-box !important;
    }
    .hero-main-title {
      font-size: clamp(1.45rem, 5.2vw, 2.75rem) !important;
      line-height: 1.2 !important;
      word-break: normal !important;
      overflow-wrap: break-word !important;
      padding: 0 0.25rem !important;
      margin-bottom: 0.65rem !important;
    }
    .hero-desc {
      font-size: clamp(0.82rem, 3.2vw, 1.02rem) !important;
      line-height: 1.5 !important;
      padding: 0 0.35rem !important;
      margin: 0 auto 1.5rem !important;
      max-width: 100% !important;
      box-sizing: border-box !important;
    }
    .capsule-search-form {
      width: 100% !important;
      max-width: 100% !important;
      box-sizing: border-box !important;
      margin: 0 auto 0.75rem !important;
    }
    .capsule-search-pill {
      flex-direction: column !important;
      border-radius: 20px !important;
      padding: 0.65rem !important;
      gap: 0.55rem !important;
      align-items: stretch !important;
      width: 100% !important;
      box-sizing: border-box !important;
    }
    .capsule-input-icon {
      display: none !important;
    }
    .capsule-url-input {
      width: 100% !important;
      max-width: 100% !important;
      box-sizing: border-box !important;
      padding: 0.35rem 0.5rem !important;
      font-size: 0.88rem !important;
      text-align: center !important;
      border: none !important;
      outline: none !important;
      text-overflow: ellipsis !important;
      overflow: hidden !important;
      white-space: nowrap !important;
    }
    .capsule-check-btn {
      width: 100% !important;
      justify-content: center !important;
      padding: 0.72rem 1.2rem !important;
      font-size: 0.88rem !important;
      box-sizing: border-box !important;
      border-radius: 9999px !important;
    }
    .hero-options-row {
      display: flex !important;
      align-items: center !important;
      justify-content: center !important;
      gap: 0.45rem !important;
      flex-wrap: wrap !important;
      width: 100% !important;
      box-sizing: border-box !important;
      padding: 0 !important;
    }
    .hero-opt-badge {
      font-size: 0.74rem !important;
      padding: 0.3rem 0.65rem !important;
      white-space: nowrap !important;
      box-sizing: border-box !important;
    }

    main.container {
      width: 100% !important;
      max-width: 100% !important;
      padding: 0 0.85rem 3rem !important;
      box-sizing: border-box !important;
      overflow: hidden !important;
    }
    .meta-strip {
      grid-template-columns: minmax(0, 1fr) !important;
      gap: 0.85rem !important;
      padding: 1rem 1.15rem !important;
      border-radius: 16px !important;
      box-sizing: border-box !important;
      width: 100% !important;
      overflow: hidden !important;
    }
    .meta-strip > div {
      min-width: 0 !important;
    }
    .meta-item-value {
      white-space: normal !important;
      word-break: break-word !important;
      overflow-wrap: anywhere !important;
      font-size: 0.84rem !important;
      line-height: 1.4 !important;
    }
    .clean-explorer-card {
      border-radius: 18px !important;
    }
    .tabs-header {
      overflow-x: auto !important;
      -webkit-overflow-scrolling: touch !important;
      white-space: nowrap !important;
      scrollbar-width: none !important;
      padding: 0.4rem 0.5rem 0 !important;
      gap: 0.35rem !important;
    }
    .tabs-header::-webkit-scrollbar {
      display: none !important;
    }
    .tab-btn {
      padding: 0.65rem 0.85rem !important;
      font-size: 0.78rem !important;
      flex-shrink: 0 !important;
    }
    .tab-panel {
      padding: 1rem 0.75rem !important;
    }
    .table-wrap {
      width: 100% !important;
      overflow-x: auto !important;
      -webkit-overflow-scrolling: touch !important;
    }
    table.data-table {
      min-width: 580px !important;
      font-size: 0.76rem !important;
    }
    table.data-table th, table.data-table td {
      padding: 0.65rem 0.6rem !important;
    }
  }

  /* Mobile Portrait (< 640px) */
  @media (max-width: 640px) {
    .top-grid {
      grid-template-columns: 1fr !important;
      gap: 1.15rem !important;
    }
    .kpi-card {
      border-radius: 20px !important;
      padding: 1.35rem 1.15rem !important;
      min-height: auto !important;
    }
    .kpi-header {
      flex-direction: column !important;
      align-items: flex-start !important;
      gap: 0.65rem !important;
    }
    .kpi-title-row {
      width: 100% !important;
    }
    .kpi-title {
      font-size: 0.92rem !important;
      white-space: normal !important;
    }
    .tier-badge {
      align-self: flex-start !important;
      font-size: 0.72rem !important;
      padding: 0.25rem 0.7rem !important;
    }
    .gauge-visual-wrap {
      flex-direction: column !important;
      align-items: center !important;
      gap: 1.15rem !important;
      margin: 0.85rem 0 1.15rem !important;
    }
    .radial-gauge {
      margin: 0 auto !important;
    }
    .sub-metrics {
      width: 100% !important;
      gap: 0.65rem !important;
    }
    .sub-metric-row {
      display: flex !important;
      justify-content: space-between !important;
      align-items: center !important;
      width: 100% !important;
      gap: 0.75rem !important;
    }
    .sub-metric-label {
      max-width: none !important;
      white-space: normal !important;
      font-size: 0.8rem !important;
    }
    .sub-metric-val {
      font-size: 0.84rem !important;
      white-space: nowrap !important;
      flex-shrink: 0 !important;
    }
    .stat-grid-2x2 {
      gap: 0.65rem !important;
    }
    .stat-box {
      padding: 0.8rem 0.9rem !important;
    }
    .stat-box-value {
      font-size: 1.4rem !important;
    }
    .stat-box-label {
      font-size: 0.72rem !important;
    }
    .card-footer-meta {
      flex-wrap: wrap !important;
      gap: 0.5rem !important;
      font-size: 0.75rem !important;
      padding-top: 0.75rem !important;
    }
  }

  /* Extra Narrow Mobile (<= 440px) */
  @media (max-width: 440px) {
    .hero-options-row {
      flex-direction: column !important;
      align-items: stretch !important;
      max-width: 280px !important;
      margin: 0.85rem auto 0 !important;
    }
    .hero-opt-badge {
      justify-content: center !important;
      width: 100% !important;
      font-size: 0.74rem !important;
    }
    .capsule-navbar {
      padding: 0.2rem 0.25rem !important;
    }
    .capsule-item:not(#navBtnMath) {
      padding: 0.32rem 0.45rem !important;
      font-size: 0.7rem !important;
    }
    #navBtnMath,
    #btnTabMath,
    #tabMath {
      display: none !important;
    }

  }

  /* =========================================================================
     MOZ-STYLE COMPREHENSIVE FOOTER (Accent Lime Theme Matching Search Bar)
     ========================================================================= */
  .site-footer {
    width: 100%;
    background: transparent;
    margin-top: 4rem;
    position: relative;
    box-sizing: border-box;
  }

  /* Main Lime Curved Wrapper (matching the search bar hero background) */
  .footer-main-wrap {
    width: 100%;
    background: var(--accent-lime);
    background-image: radial-gradient(circle at 50% 15%, rgba(255, 255, 255, 0.45) 0%, rgba(217, 249, 66, 0) 70%),
                      linear-gradient(180deg, #dcfc40 0%, #cbee26 100%);
    border-top: 1px solid rgba(15, 23, 42, 0.08);
    border-top-left-radius: 50% 45px;
    border-top-right-radius: 50% 45px;
    padding: 3.5rem 1.5rem 4rem;
    box-shadow: 0 -12px 36px rgba(217, 249, 66, 0.28);
    box-sizing: border-box;
    overflow: hidden;
  }

  /* Top CTA Capsule Pill Banner (Inside Lime Background) */
  .footer-cta-wrap {
    width: 96%;
    max-width: 1860px;
    margin: 0 auto 3rem;
    padding-bottom: 2.75rem;
    border-bottom: 1px solid rgba(15, 23, 42, 0.15);
    box-sizing: border-box;
  }

  .footer-cta-card {
    background: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.9);
    border-radius: 9999px;
    box-shadow: 0 10px 25px rgba(0, 0, 0, 0.1);
    padding: 0.95rem 1.25rem 0.95rem 2rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1.25rem;
    flex-wrap: nowrap;
    width: 100%;
    box-sizing: border-box;
  }

  .footer-cta-left {
    display: flex;
    align-items: center;
    gap: 1rem;
    min-width: 0;
    flex: 1 1 auto;
    flex-wrap: nowrap;
  }

  .footer-cta-title {
    font-size: 1.35rem;
    font-weight: 800;
    color: #0f172a;
    letter-spacing: -0.02em;
    margin: 0;
    font-family: 'Outfit', sans-serif;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .footer-cta-subtitle {
    font-size: 0.9rem;
    color: #334155;
    font-weight: 500;
    line-height: 1.35;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }

  .footer-cta-right {
    flex-shrink: 0;
  }

  .footer-cta-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: var(--nav-dark);
    color: #ffffff;
    font-weight: 700;
    font-size: 0.9rem;
    padding: 0.75rem 1.75rem;
    border-radius: 9999px;
    text-decoration: none;
    box-shadow: 0 4px 14px rgba(23, 27, 38, 0.35);
    transition: all 0.2s ease;
    white-space: nowrap;
    border: 1px solid #282f3f;
    cursor: pointer;
    flex-shrink: 0;
  }

  .footer-cta-btn:hover {
    background: #000000;
    transform: translateY(-2px);
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.45);
    color: #ffffff;
  }

  .footer-cta-btn:disabled, .footer-cta-btn.disabled {
    cursor: not-allowed !important;
    opacity: 0.85 !important;
    pointer-events: none !important;
    transform: none !important;
    box-shadow: 0 4px 14px rgba(23, 27, 38, 0.25) !important;
  }

  .footer-main-container {
    width: 96%;
    max-width: 1860px;
    margin: 0 auto;
    display: grid;
    grid-template-columns: 1fr 1fr 1fr 1.25fr;
    gap: 3.5rem;
    box-sizing: border-box;
  }

  .footer-col {
    display: flex;
    flex-direction: column;
  }

  .footer-col-header {
    font-size: 0.82rem;
    font-weight: 800;
    color: #0f172a;
    letter-spacing: 0.06em;
    margin-bottom: 1.15rem;
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-family: 'Outfit', sans-serif;
  }

  .footer-arrow {
    color: #0f172a;
    font-size: 0.85rem;
    font-weight: 900;
  }

  .footer-sub-header {
    margin-top: 2rem;
  }

  .footer-links-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.65rem;
  }

  .footer-links-list li a {
    font-size: 0.86rem;
    color: #1e293b;
    text-decoration: none;
    font-weight: 600;
    line-height: 1.35;
    transition: color 0.15s ease, transform 0.15s ease;
    display: inline-block;
  }

  .footer-links-list li a:hover {
    color: #000000;
    text-decoration: underline;
  }

  /* Coming Soon Badge & Description Box in Footer */
  .footer-coming-soon-box {
    margin-top: 0.65rem;
    padding: 0.75rem 0.9rem;
    background: rgba(255, 255, 255, 0.65);
    border: 1px dashed rgba(15, 23, 42, 0.28);
    border-radius: 12px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
    max-width: 250px;
  }

  .footer-coming-soon-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    background: var(--nav-dark);
    color: #ffffff;
    font-size: 0.68rem;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 0.22rem 0.6rem;
    border-radius: 9999px;
    margin-bottom: 0.35rem;
  }

  .pulse-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent-lime);
    box-shadow: 0 0 6px rgba(217, 249, 66, 0.9);
    display: inline-block;
  }

  .footer-coming-soon-desc {
    font-size: 0.76rem;
    color: #1e293b;
    line-height: 1.45;
    font-weight: 600;
    margin: 0;
  }

  /* Col 6: Connect with us */
  .footer-connect-col {
    border-left: 1px solid rgba(15, 23, 42, 0.18);
    padding-left: 2.25rem;
  }

  .footer-connect-title {
    font-size: 1.25rem;
    font-weight: 800;
    color: #0f172a;
    margin-bottom: 1.25rem;
    font-family: 'Outfit', sans-serif;
    letter-spacing: -0.01em;
  }

  .footer-social-icons {
    display: flex;
    gap: 0.75rem;
    align-items: center;
    margin-bottom: 1.35rem;
    flex-wrap: wrap;
  }

  .social-icon-btn {
    width: 38px;
    height: 38px;
    border-radius: 50%;
    background: #ffffff;
    border: 1px solid rgba(15, 23, 42, 0.14);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: #0f172a;
    transition: all 0.2s ease;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.06);
    text-decoration: none;
  }

  .social-icon-btn:hover {
    color: #ffffff;
    border-color: #171b26;
    background: #171b26;
    transform: translateY(-2px);
    box-shadow: 0 4px 10px rgba(0, 0, 0, 0.2);
  }

  .footer-help-link-wrap {
    margin-top: 0.5rem;
  }

  .footer-help-link {
    color: #0f172a;
    font-size: 0.94rem;
    font-weight: 800;
    text-decoration: underline;
    transition: all 0.15s ease;
    display: inline-block;
  }

  .footer-help-link:hover {
    color: #000000;
  }

  /* Bottom Dark Bar */
  .footer-bottom-bar {
    width: 100%;
    background: var(--nav-dark);
    border-top: 1px solid #232938;
    padding: 1.65rem 1.5rem;
    box-sizing: border-box;
  }

  .footer-bottom-container {
    width: 96%;
    max-width: 1860px;
    margin: 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1.75rem;
    flex-wrap: wrap;
  }

  .footer-bottom-logo {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .footer-bottom-copy {
    font-size: 0.78rem;
    color: #94a3b8;
    text-align: center;
    flex: 1;
    min-width: 280px;
    line-height: 1.5;
  }

  .footer-bottom-legal {
    display: flex;
    align-items: center;
    gap: 1.5rem;
    flex-wrap: wrap;
  }

  .footer-bottom-legal a {
    color: #94a3b8;
    text-decoration: none;
    font-size: 0.78rem;
    font-weight: 500;
    transition: color 0.15s ease;
  }

  .footer-bottom-legal a:hover {
    color: #ffffff;
    text-decoration: underline;
  }

  /* Responsive footer breakpoints */
  @media (max-width: 1024px) {
    .footer-main-container {
      grid-template-columns: repeat(2, 1fr);
      gap: 2.5rem;
    }
    .footer-connect-col {
      border-left: none;
      padding-left: 0;
      border-top: 1px solid rgba(15, 23, 42, 0.15);
      padding-top: 2rem;
      grid-column: 1 / -1;
    }
    .footer-cta-left {
      flex-wrap: wrap;
      gap: 0.35rem 0.85rem;
    }
    .footer-cta-subtitle {
      white-space: normal;
      overflow: visible;
      text-overflow: clip;
    }
  }

  @media (max-width: 768px) {
    .authority-overview-card {
      padding: 2.25rem 1.25rem !important;
    }
    .authority-feature-highlights {
      grid-template-columns: 1fr !important;
    }
    .footer-cta-card {
      padding: 1.35rem 1.25rem;
      border-radius: 22px;
      flex-direction: column;
      align-items: flex-start;
      gap: 1rem;
    }
    .footer-cta-left {
      flex-direction: column;
      align-items: flex-start;
      flex-wrap: wrap;
      gap: 0.45rem;
      width: 100%;
    }
    .footer-cta-title {
      font-size: 1.25rem;
      white-space: normal;
      line-height: 1.25;
    }
    .footer-cta-subtitle {
      font-size: 0.88rem;
      white-space: normal;
      overflow: visible;
      text-overflow: clip;
      line-height: 1.45;
    }
    .footer-cta-right {
      width: 100%;
    }
    .footer-cta-btn {
      width: 100%;
      text-align: center;
    }
    .footer-main-wrap {
      border-top-left-radius: 50% 28px;
      border-top-right-radius: 50% 28px;
      padding: 2.75rem 1rem 2.25rem;
    }
    .footer-main-container {
      grid-template-columns: 1fr;
      gap: 2rem;
    }
    .footer-bottom-container {
      flex-direction: column;
      text-align: center;
      gap: 1rem;
    }
    .footer-bottom-legal {
      justify-content: center;
      gap: 1rem;
    }
  }

  </style>
</head>
<body>

  <!-- Floating Navigation Bar with Image 2 Signature Capsule + RW Logo -->
  <header class="nav-container">
    <a href="https://rizemetricstool.com/" class="brand-group" style="text-decoration:none;">
      <!-- RW Uploaded Logo -->
      <img src="/logo.png" alt="RizeMetrics Logo" class="brand-logo-img" />
      <div>
        <span class="brand-title"><span class="brand-rize">Rize</span><span class="brand-metrics">Metrics</span></span>
        <span class="brand-subtitle">Graph Authority &amp; PageRank Engine</span>
      </div>
    </a>

    <!-- Center Floating Capsule Nav -->
    <nav class="capsule-navbar">
      <button id="navBtnAuthority" class="capsule-item active" onclick="openAuthorityTab()">
        <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/>
        </svg>
        Authority
      </button>
      <button id="navBtnBacklinks" class="capsule-item" onclick="openBacklinksTab()">
        Backlinks
      </button>
      <button id="navBtnSpam" class="capsule-item" onclick="openSpamTab()">
        Spam Audit
      </button>
      <button id="navBtnMath" class="capsule-item" hidden style="display: none !important; visibility: hidden !important; width: 0 !important; height: 0 !important; padding: 0 !important; margin: 0 !important; border: none !important; position: absolute !important; opacity: 0 !important; pointer-events: none !important;" onclick="switchTab('tabMath', document.getElementById('btnTabMath'))">
        Math Proof
      </button>
    </nav>

    <div class="nav-actions">
      <button type="button" class="quick-export-btn" onclick="window.print()" title="Print or save PDF report">
        <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
        </svg>
        <span>Export</span>
      </button>
    </div>
  </header>

  <!-- Moz-Style Curved Hero Banner with Accent Lime Green Background -->
  <section class="curved-hero-section">
    <div class="hero-inner-container">
      <h1 class="hero-main-title">Free Domain Authority Checker</h1>
      <p class="hero-desc">
        Check the Domain Authority&reg;, Page Authority, ranking keywords, and top search competitors for a domain with a single click &mdash; no login required.
      </p>

      <form id="checkForm" class="capsule-search-form" onsubmit="handleCheckSubmit(event)">
        <div class="capsule-search-pill">
          <span class="capsule-input-icon">
            <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
              <circle cx="11" cy="11" r="8"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
          </span>
          <input
            type="text"
            id="urlInput"
            class="capsule-url-input"
            placeholder="Enter domain or bulk URLs (comma/space separated)..."
            value=""
            required
            autocomplete="off"
          />
          <input
            type="file"
            id="csvFileInput"
            accept=".csv,.txt"
            style="display: none;"
            onchange="handleCsvFileUpload(event)"
          />
          <button
            type="button"
            id="uploadCsvBtn"
            class="capsule-csv-btn"
            onclick="document.getElementById('csvFileInput').click()"
            title="Upload CSV or TXT file with bulk URLs"
          >
            <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"/>
            </svg>
            <span>Upload CSV</span>
          </button>
          <button type="submit" id="checkBtn" class="capsule-check-btn">
            <span>Analyze Authority</span>
            <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" d="M14 5l7 7m0 0l-7 7m7-7H3"/>
            </svg>
          </button>
        </div>

        <div class="hero-options-row">
          <label class="hero-opt-badge" title="Fetch live HTML and crawl internal links">
            <input type="checkbox" id="optLiveCrawl" checked style="accent-color: #0f172a;" />
            <span>Live Mini-Crawler</span>
          </label>
          <label class="hero-opt-badge" title="Query Internet Archive Wayback CDX">
            <input type="checkbox" id="optArchive" style="accent-color: #0f172a;" />
            <span>Wayback CDX History</span>
          </label>
          <label class="hero-opt-badge">
            <span>Max Internal Pages:</span>
            <select id="optMaxPages" class="hero-select">
              <option value="1" selected>1 page (Fastest)</option>
              <option value="3">3 pages</option>
              <option value="5">5 pages (Balanced)</option>
              <option value="10">10 pages (Deep)</option>
            </select>
          </label>
        </div>
      </form>
    </div>
  </section>

  <main class="container">

    <!-- Loading Progress Indicator -->
    <div id="loadingBanner" class="loading-banner">
      <div class="spinner"></div>
      <div>
        <div style="font-weight: 700; color: var(--primary);" id="loadingStepTitle">Analyzing URL &amp; Building Web Graph...</div>
        <div style="font-size: 0.82rem; color: var(--text-soft);" id="loadingStepSub">
          Extracting internal/external links, querying Wayback CDX &amp; solving PageRank...
        </div>
      </div>
    </div>

    <!-- Error Alert -->
    <div id="errorBanner" class="error-banner"></div>

    <!-- Bulk URL Authority Checker Results Section (DA, PA, Spam Score Only) -->
    <div id="bulkResultsSection" style="display: none;">
      <div class="bulk-results-card">
        <div class="bulk-header-row">
          <div class="bulk-title-group">
            <h2>
              <span>Bulk URL Authority &amp; Spam Score Report</span>
              <span style="font-size: 0.75rem; background: #dcfce7; color: #166534; padding: 0.22rem 0.65rem; border-radius: 999px; font-weight: 700;">Unique MongoDB Sync</span>
            </h2>
            <p id="bulkSubtitleText">Showing Domain Authority (DA), Page Authority (PA), and Spam Score for each unique URL.</p>
          </div>
          <div class="bulk-actions-group">
            <button type="button" class="bulk-export-btn" onclick="exportBulkResultsCsv()">
              <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
              </svg>
              <span>Export CSV</span>
            </button>
            <button type="button" class="bulk-clear-btn" onclick="resetView()">Clear Results</button>
          </div>
        </div>

        <div class="bulk-summary-strip">
          <div class="bulk-stat-chip">
            <span class="bulk-stat-label">Unique URLs Analyzed</span>
            <span class="bulk-stat-val" id="bulkStatUnique">0</span>
          </div>
          <div class="bulk-stat-chip">
            <span class="bulk-stat-label">Duplicates Skipped</span>
            <span class="bulk-stat-val" id="bulkStatDuplicates">0</span>
          </div>
          <div class="bulk-stat-chip">
            <span class="bulk-stat-label">Average DA</span>
            <span class="bulk-stat-val" id="bulkStatAvgDa">0.0</span>
          </div>
          <div class="bulk-stat-chip">
            <span class="bulk-stat-label">Average PA</span>
            <span class="bulk-stat-val" id="bulkStatAvgPa">0.0</span>
          </div>
          <div class="bulk-stat-chip">
            <span class="bulk-stat-label">Average Spam Score</span>
            <span class="bulk-stat-val" id="bulkStatAvgSpam">1.0%</span>
          </div>
        </div>

        <div class="bulk-table-wrap">
          <table class="bulk-table">
            <thead>
              <tr>
                <th style="width: 60px;">#</th>
                <th>URL / Domain</th>
                <th style="text-align: center;">Domain Authority (DA)</th>
                <th style="text-align: center;">Page Authority (PA)</th>
                <th style="text-align: center;">Spam Score</th>
              </tr>
            </thead>
            <tbody id="bulkResultsTableBody"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Results Container (Shown only after user enters a link and runs analysis) -->
    <div id="resultsSection" style="display: none;">

      <!-- Top 4 Bento KPI Cards (FULL PASTEL THEMES MATCHING USER SCREENSHOT) -->
      <div class="top-grid">

        <!-- CARD 1: DOMAIN AUTHORITY (LP-DA) - PASTEL PEACH / ORANGE -->
        <div class="kpi-card card-da">
          <div>
            <div class="kpi-header">
              <div class="kpi-title-row">
                <div class="kpi-icon-pill">
                  <!-- Shield Icon -->
                  <svg width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/>
                  </svg>
                </div>
                <div class="kpi-title-text">
                  <div class="kpi-title">Domain Authority</div>
                  <div class="kpi-subtitle" id="daDomainLabel">zoniraz.com</div>
                </div>
              </div>
            </div>

            <div class="gauge-visual-wrap">
              <div class="radial-gauge">
                <svg viewBox="0 0 120 120">
                  <circle class="gauge-bg" cx="60" cy="60" r="50"></circle>
                  <circle id="daGaugeCircle" class="gauge-fill" cx="60" cy="60" r="50"
                          stroke="#ea580c" stroke-dasharray="314.16" stroke-dashoffset="314.16"></circle>
                </svg>
                <div class="gauge-center-text">
                  <span class="gauge-number" id="daScoreNum">0.0</span>
                  <span class="gauge-scale">/ 100 DA</span>
                </div>
              </div>

              <div class="sub-metrics">
                <div class="sub-metric-item">
                  <div class="sub-metric-row">
                    <span class="sub-metric-label">Anchor Root Authority</span>
                    <span class="sub-metric-val mono" id="daAnchorVal">0.0</span>
                  </div>
                  <div class="mini-bar-bg"><div id="daAnchorBar" class="mini-bar-fill" style="width:0%; background:#ea580c;"></div></div>
                </div>

                <div class="sub-metric-item">
                  <div class="sub-metric-row">
                    <span class="sub-metric-label">Top Harmonic Authority</span>
                    <span class="sub-metric-val mono" id="daTopKVal">0.0</span>
                  </div>
                  <div class="mini-bar-bg"><div id="daTopKBar" class="mini-bar-fill" style="width:0%; background:#ea580c;"></div></div>
                </div>

                <div class="sub-metric-item">
                  <div class="sub-metric-row">
                    <span class="sub-metric-label">Domain Breadth Score</span>
                    <span class="sub-metric-val mono" id="daBreadthVal">0.0</span>
                  </div>
                  <div class="mini-bar-bg"><div id="daBreadthBar" class="mini-bar-fill" style="width:0%; background:#ea580c;"></div></div>
                </div>

                <div class="sub-metric-item">
                  <div class="sub-metric-row">
                    <span class="sub-metric-label">Mean Page Authority</span>
                    <span class="sub-metric-val mono" id="daMeanVal">0.0</span>
                  </div>
                  <div class="mini-bar-bg"><div id="daMeanBar" class="mini-bar-fill" style="width:0%; background:#ea580c;"></div></div>
                </div>
              </div>
            </div>
          </div>

          <div class="card-footer-meta">
            <span id="daNormalizedLabel" class="mono">LP-DA: 0.000000</span>
            <span id="daScoredPagesLabel">Scored Pages: 0</span>
          </div>
        </div>

        <!-- CARD 2: PAGE AUTHORITY (LP-PA) - PASTEL SKY BLUE -->
        <div class="kpi-card card-pa">
          <div>
            <div class="kpi-header">
              <div class="kpi-title-row">
                <div class="kpi-icon-pill">
                  <!-- Document File Icon -->
                  <svg width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                  </svg>
                </div>
                <div class="kpi-title-text">
                  <div class="kpi-title">Page Authority</div>
                  <div class="kpi-subtitle" id="paUrlLabel">Root Target URL</div>
                </div>
              </div>
            </div>

            <div class="gauge-visual-wrap">
              <div class="radial-gauge">
                <svg viewBox="0 0 120 120">
                  <circle class="gauge-bg" cx="60" cy="60" r="50"></circle>
                  <circle id="paGaugeCircle" class="gauge-fill" cx="60" cy="60" r="50"
                          stroke="#2563eb" stroke-dasharray="314.16" stroke-dashoffset="314.16"></circle>
                </svg>
                <div class="gauge-center-text">
                  <span class="gauge-number" id="paScoreNum">0.0</span>
                  <span class="gauge-scale">/ 100 PA</span>
                </div>
              </div>

              <div class="sub-metrics">
                <div class="sub-metric-item">
                  <div class="sub-metric-row">
                    <span class="sub-metric-label">Raw PageRank Score</span>
                    <span class="sub-metric-val mono" id="paRawPrVal">0.0000</span>
                  </div>
                </div>
                <div class="sub-metric-item">
                  <div class="sub-metric-row">
                    <span class="sub-metric-label">Component Scale Factor</span>
                    <span class="sub-metric-val mono" id="paCompScaleVal">0.0000</span>
                  </div>
                </div>
                <div class="sub-metric-item">
                  <div class="sub-metric-row">
                    <span class="sub-metric-label">Web Graph Size</span>
                    <span class="sub-metric-val mono" id="paWccSizeVal">0 nodes</span>
                  </div>
                </div>
                <div class="sub-metric-item">
                  <div class="sub-metric-row">
                    <span class="sub-metric-label">Solver Iterations</span>
                    <span class="sub-metric-val mono" id="paIterVal">0 iters</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div class="card-footer-meta">
            <span id="paNormalizedLabel" class="mono">LP-PA: 0.000000</span>
            <span id="paConvergedLabel" style="color:#1d4ed8; font-weight:700;">Converged</span>
          </div>
        </div>

        <!-- CARD 3: MOZ SPAM SCORE (27-SIGNAL) - PASTEL MINT GREEN -->
        <div class="kpi-card card-spam">
          <div>
            <div class="kpi-header">
              <div class="kpi-title-row">
                <div class="kpi-icon-pill">
                  <!-- Alert Triangle Icon -->
                  <svg width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>
                  </svg>
                </div>
                <div class="kpi-title-text">
                  <div class="kpi-title">Spam Risk Score</div>
                  <div class="kpi-subtitle" id="spamCardSubtitle">27-Signal Diagnostic</div>
                </div>
              </div>
              <span class="tier-badge" id="spamCardRiskBadge">Low Risk</span>
            </div>

            <div class="gauge-visual-wrap">
              <div class="radial-gauge">
                <svg viewBox="0 0 120 120">
                  <circle class="gauge-bg" cx="60" cy="60" r="50"></circle>
                  <circle id="spamGaugeCircle" class="gauge-fill" cx="60" cy="60" r="50"
                          stroke="#059669" stroke-dasharray="314.16" stroke-dashoffset="314.16"></circle>
                </svg>
                <div class="gauge-center-text">
                  <span class="gauge-number" id="spamScoreNum">1%</span>
                  <span class="gauge-scale">SPAM SCORE</span>
                </div>
              </div>

              <div class="sub-metrics">
                <div class="sub-metric-item">
                  <div class="sub-metric-row">
                    <span class="sub-metric-label">Triggered Spam Signals</span>
                    <span class="sub-metric-val mono" id="spamFlaggedCountVal">0 / 27</span>
                  </div>
                  <div class="mini-bar-bg"><div id="spamFlaggedBar" class="mini-bar-fill" style="width:0%; background:#059669;"></div></div>
                </div>
                <div class="sub-metric-item">
                  <div class="sub-metric-row">
                    <span class="sub-metric-label">Domain Trust Score</span>
                    <span class="sub-metric-val mono" id="spamMozTrustVal">0.0 / 10</span>
                  </div>
                </div>
                <div class="sub-metric-item">
                  <div class="sub-metric-row">
                    <span class="sub-metric-label">Domain Rank Score</span>
                    <span class="sub-metric-val mono" id="spamMozRankVal">0.0 / 10</span>
                  </div>
                </div>
                <div class="sub-metric-item">
                  <div class="sub-metric-row">
                    <span class="sub-metric-label">Authority Penalty</span>
                    <span class="sub-metric-val mono" id="spamPenaltyVal">0.0%</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div class="card-footer-meta">
            <span id="spamBandNote">Low (1-30%) • High (61%+)</span>
            <button type="button" class="white-pill-btn" onclick="openSpamTab()">Audit &rarr;</button>
          </div>
        </div>

      </div>

      <!-- CARD 4: LINK PROFILE & TELEMETRY - WIDE BANNER (Shifted below top 3 cards, covering full width with compact height) -->
      <div class="kpi-card card-links card-telemetry-wide">
        <div class="telemetry-row-top">
          <div class="kpi-title-row">
            <div class="kpi-icon-pill">
              <!-- Network / Share Icon -->
              <svg width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/>
              </svg>
            </div>
            <div class="kpi-title-text">
              <div class="kpi-title">Link Telemetry</div>
              <div class="kpi-subtitle" id="linkSourcesLabel">Live Discovered Graph</div>
            </div>
            <span class="tier-badge" id="execTimeBadge">0 ms</span>
          </div>

          <div class="telemetry-meta-right">
            <span id="statRefDomains">Ref Domains: 0</span>
            <span id="statWaybackSnaps">Archive: 0</span>
            <button type="button" class="white-pill-btn" onclick="openBacklinksTab()">Backlinks &rarr;</button>
          </div>
        </div>

        <div class="telemetry-row-bottom">
          <div class="telemetry-stats-group">
            <div class="stat-box">
              <div class="stat-box-value mono" id="statTotalLinks">0</div>
              <div class="stat-box-label">Total Links</div>
            </div>
            <div class="stat-box">
              <div class="stat-box-value mono" id="statInternalLinks" style="color:#2563eb;">0</div>
              <div class="stat-box-label">Internal</div>
            </div>
            <div class="stat-box">
              <div class="stat-box-value mono" id="statInboundLinks" style="color:#059669;">0</div>
              <div class="stat-box-label">Backlinks</div>
            </div>
            <div class="stat-box">
              <div class="stat-box-value mono" id="statExternalLinks" style="color:#7c3aed;">0</div>
              <div class="stat-box-label">External</div>
            </div>
          </div>

          <div class="telemetry-dofollow-group">
            <div class="sub-metric-row">
              <span class="sub-metric-label">Dofollow vs Nofollow</span>
              <span class="sub-metric-val mono" id="statRelRatioVal">100% Dofollow</span>
            </div>
            <div class="mini-bar-bg">
              <div id="statRelBar" class="mini-bar-fill" style="width:100%; background:linear-gradient(90deg, #059669, #7c3aed);"></div>
            </div>
          </div>
        </div>
      </div>

      <!-- Target Page Metadata Strip -->
      <div class="meta-strip">
        <div>
          <div class="meta-item-label">Target Page Title</div>
          <div class="meta-item-value" id="metaTitle">-</div>
        </div>
        <div>
          <div class="meta-item-label">HTTP Status &amp; Canonical</div>
          <div class="meta-item-value mono" id="metaHttpStatus">200 OK</div>
        </div>
        <div>
          <div class="meta-item-label">Primary H1 Heading</div>
          <div class="meta-item-value" id="metaH1">-</div>
        </div>
        <div>
          <div class="meta-item-label">Trust &amp; Spam Diagnostics</div>
          <div class="meta-item-value" id="metaGraphVE">Trust: - | Spam: 1%</div>
        </div>
      </div>

      <!-- =========================================================================
           CLEAN LIGHT TABLE EXPLORER (The clean bottom section)
           ========================================================================= -->
      <section class="clean-explorer-card">
        <div class="tabs-header">
          <button id="btnTabPages" class="tab-btn active" onclick="switchTab('tabPages', this)">
            <span>Top Constituent Pages</span>
            <span class="tab-badge" id="countPagesTab">0</span>
          </button>
          <button id="btnTabInbound" class="tab-btn" onclick="switchTab('tabInbound', this)">
            <span>Inbound Backlinks</span>
            <span class="tab-badge" id="countInboundTab">0</span>
          </button>
          <button class="tab-btn" onclick="switchTab('tabInternal', this)">
            <span>Internal Links</span>
            <span class="tab-badge" id="countInternalTab">0</span>
          </button>
          <button class="tab-btn" onclick="switchTab('tabExternal', this)">
            <span>External Outbound</span>
            <span class="tab-badge" id="countExternalTab">0</span>
          </button>
          <button id="tabSpamBtn" class="tab-btn" onclick="switchTab('tabSpam', this)">
            <span>27-Signal Spam Audit</span>
            <span class="tab-badge" id="countSpamTab">0</span>
          </button>
          <button id="btnTabMath" class="tab-btn" hidden style="display: none !important; visibility: hidden !important; width: 0 !important; height: 0 !important; padding: 0 !important; margin: 0 !important; border: none !important; position: absolute !important; opacity: 0 !important; pointer-events: none !important;" onclick="switchTab('tabMath', this)">
            <span>Math Proof &amp; Audit</span>
          </button>
        </div>

        <!-- TAB 1: TOP CONSTITUENT PAGES -->
        <div id="tabPages" class="tab-panel active">
          <div class="table-wrap">
            <table class="data-table">
              <thead>
                <tr>
                  <th>Rank</th>
                  <th>Constituent Page URL</th>
                  <th>Page Authority (0–100)</th>
                  <th>LP-PA (0–1)</th>
                  <th>Raw PageRank</th>
                  <th>In / Out Links</th>
                  <th>Source</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody id="tbodyConstituentPages"></tbody>
            </table>
          </div>
        </div>

        <!-- TAB 2: INBOUND BACKLINKS -->
        <div id="tabInbound" class="tab-panel">
          <div id="inboundSummaryBanner" style="display:flex; flex-wrap:wrap; gap:12px; margin-bottom:14px; padding:12px 14px; background:#f8fafc; border:1px solid var(--border-subtle); border-radius:10px; align-items:center; justify-content:space-between;">
            <div style="font-size:0.84rem; color:var(--text-muted);">
              Total Backlinks Built: <strong id="inboundTotalBuiltVal" style="color:#059669;" class="mono">0</strong> &bull;
              Referring Sites (Domains): <strong id="inboundTotalLrdVal" style="color:#2563eb;" class="mono">0</strong>
            </div>
            <div id="inboundSampleSitesText" style="font-size:0.78rem; color:var(--text-soft);"></div>
          </div>
          <div class="table-wrap" style="margin-bottom:16px;">
            <table class="data-table">
              <thead>
                <tr>
                  <th>Referring Site (Domain)</th>
                  <th>Backlinks Found</th>
                  <th>Link Type</th>
                  <th>Source / Provenance</th>
                </tr>
              </thead>
              <tbody id="tbodyReferringSites"></tbody>
            </table>
          </div>
          <div class="table-wrap">
            <table class="data-table">
              <thead>
                <tr>
                  <th>Referring Source URL</th>
                  <th>Referring Domain</th>
                  <th>Target URL</th>
                  <th>Anchor Text</th>
                  <th>Rel Attribute</th>
                  <th>DOM Context</th>
                  <th>Provenance</th>
                </tr>
              </thead>
              <tbody id="tbodyInboundLinks"></tbody>
            </table>
          </div>
        </div>

        <!-- TAB 3: INTERNAL LINKS -->
        <div id="tabInternal" class="tab-panel">
          <div class="table-wrap">
            <table class="data-table">
              <thead>
                <tr>
                  <th>Source Page</th>
                  <th>Internal Target URL</th>
                  <th>Anchor Text</th>
                  <th>Rel Attribute</th>
                  <th>DOM Context</th>
                </tr>
              </thead>
              <tbody id="tbodyInternalLinks"></tbody>
            </table>
          </div>
        </div>

        <!-- TAB 4: EXTERNAL LINKS -->
        <div id="tabExternal" class="tab-panel">
          <div class="table-wrap">
            <table class="data-table">
              <thead>
                <tr>
                  <th>Source Page</th>
                  <th>External Target URL</th>
                  <th>Target Domain</th>
                  <th>Anchor Text</th>
                  <th>Rel Attribute</th>
                  <th>DOM Context</th>
                </tr>
              </thead>
              <tbody id="tbodyExternalLinks"></tbody>
            </table>
          </div>
        </div>

        <!-- TAB 5: SPAM AUDIT -->
        <div id="tabSpam" class="tab-panel">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:1rem; flex-wrap:wrap; gap:0.5rem;">
            <span class="tier-badge" id="spamSummaryBadge" style="background:#ecfdf5; color:#059669; border:1px solid #a7f3d0; font-size:0.8rem; padding:0.35rem 0.8rem;">
              Spam Score: 1% (Low Risk)
            </span>
            <span style="font-size:0.75rem; color:var(--text-soft);">Full 27-signal webspam diagnostic audit</span>
          </div>
          <div class="table-wrap">
            <table class="data-table">
              <thead>
                <tr>
                  <th>Signal ID</th>
                  <th>Diagnostic Name</th>
                  <th>Category</th>
                  <th>Evaluation Status</th>
                  <th>Signal Detail &amp; Threshold</th>
                </tr>
              </thead>
              <tbody id="tbodySpamAudit"></tbody>
            </table>
          </div>
        </div>

        <!-- TAB 6: MATHEMATICAL PROOF -->
        <div id="tabMath" class="tab-panel">
          <div class="formula-box" id="mathAuditBox">
            Mathematical proof will render upon evaluation.
          </div>
        </div>

      </section>

    </div>

    <!-- Recent Session History -->
    <section class="history-section" id="historySection" style="display: none;">
      <h3 class="history-title">Recent Session Analyses</h3>
      <div class="history-grid" id="historyGrid"></div>
    </section>

    <!-- =========================================================================
         BRAND AUTHORITY OVERVIEW SECTION (Between Hero and Footer)
         ========================================================================= -->
    <section class="authority-overview-section" id="authorityOverviewSection">
      <div class="authority-overview-card">
        <div class="authority-overview-badge">
          <span class="pulse-dot"></span>
          <span>FREE DA &amp; PA CHECKER</span>
        </div>
        <h2 class="authority-overview-title">
          Free Website Authority Checker by RizeMetrics for DA PA and Spam Score
        </h2>
        <p class="authority-overview-desc">
          Check your website’s Domain Authority (DA), Page Authority (PA), and Spam Score instantly with our free DA PA Checker. Analyze up to Unlimited website URLs in one go, get accurate SEO metrics. No sign-up, no login, and no daily checking limits required.
        </p>

        <div class="authority-feature-highlights">
          <div class="authority-highlight-box">
            <div class="highlight-icon-wrap" style="background: rgba(37, 99, 235, 0.1); color: #2563eb;">
              <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z"/>
              </svg>
            </div>
            <div>
              <h4 class="highlight-title">Instant DA, PA &amp; Spam Score</h4>
              <p class="highlight-sub">Evaluate root Domain Authority, target Page Authority, and 27-signal Spam Score in real-time.</p>
            </div>
          </div>

          <div class="authority-highlight-box">
            <div class="highlight-icon-wrap" style="background: rgba(16, 185, 129, 0.1); color: #059669;">
              <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/>
              </svg>
            </div>
            <div>
              <h4 class="highlight-title">Unlimited URL Analyses</h4>
              <p class="highlight-sub">Run unlimited website audits without daily quota limits, artificial caps, or paywalls.</p>
            </div>
          </div>

          <div class="authority-highlight-box">
            <div class="highlight-icon-wrap" style="background: rgba(217, 119, 6, 0.1); color: #d97706;">
              <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/>
              </svg>
            </div>
            <div>
              <h4 class="highlight-title">No Sign-Up or Login</h4>
              <p class="highlight-sub">100% free with no registration required &mdash; analyze any web domain directly in your browser.</p>
            </div>
          </div>
        </div>
      </div>
    </section>

  </main>

  <!-- =========================================================================
       MOZ-STYLE COMPREHENSIVE FOOTER (Accent Lime Theme Matching Search Bar)
       ========================================================================= -->
  <footer class="site-footer">
    <!-- Main Lime Curved Section (matching Search Bar Hero Banner) -->
    <div class="footer-main-wrap">
      <!-- Top CTA Banner -->
      <div class="footer-cta-wrap">
        <div class="footer-cta-card">
          <div class="footer-cta-left">
            <h2 class="footer-cta-title">Get started with RizeMetrics!</h2>
            <span class="footer-cta-subtitle">AI-powered, fully automated SEO tool for complete backlink workflows to reduce manual effort.</span>
          </div>
          <div class="footer-cta-right">
            <button type="button" class="footer-cta-btn disabled" disabled style="cursor: not-allowed; opacity: 0.85;">Coming Soon</button>
          </div>
        </div>
      </div>

      <!-- Main Multi-Column Links Section -->
      <div class="footer-main-container">
        <!-- Col 1: Products -->
        <div class="footer-col">
          <div class="footer-col-header">
            <span>PRODUCTS</span>
            <span class="footer-arrow">&rarr;</span>
          </div>
          <ul class="footer-links-list">
            <li><a href="javascript:void(0)" onclick="const inp=document.getElementById('urlInput'); if(inp){ inp.focus(); window.scrollTo({top:0, behavior:'smooth'}); }">Domain Authority (LP-DA)</a></li>
            <li><a href="javascript:void(0)" onclick="const inp=document.getElementById('urlInput'); if(inp){ inp.focus(); window.scrollTo({top:0, behavior:'smooth'}); }">Page Authority (LP-PA)</a></li>
            <li class="footer-coming-soon-box">
              <span class="footer-coming-soon-pill">
                <span class="pulse-dot"></span> Coming Soon
              </span>
              <p class="footer-coming-soon-desc">
                Exciting new SEO &amp; AI-driven intelligence products are currently in development &mdash; launching soon!
              </p>
            </li>
          </ul>
        </div>

        <!-- Col 2: Resources -->
        <div class="footer-col">
          <div class="footer-col-header">
            <span>RESOURCES</span>
          </div>
          <ul class="footer-links-list">
            <li><a href="/blog">Blog</a></li>
            <li><a href="/seo-learning-center">SEO Learning Center</a></li>
            <li><a href="/spam-guide">27-Signal Spam Guide</a></li>
            <li><a href="/beginners-guide">Beginner's Guide to SEO</a></li>
            <li><a href="/how-to-guides">How-to Guides</a></li>
          </ul>
        </div>

        <!-- Col 3: About -->
        <div class="footer-col">
          <div class="footer-col-header">
            <span>ABOUT RIZEMETRICS</span>
          </div>
          <ul class="footer-links-list">
            <li><a href="/about">About Us</a></li>
            <li><a href="/contact">Contact</a></li>
          </ul>
        </div>

        <!-- Col 4: Connect with us (Divider + Social Icons) -->
        <div class="footer-col footer-connect-col">
          <h3 class="footer-connect-title">Connect with us</h3>
          <div class="footer-social-icons">
            <a href="https://www.facebook.com/share/1DSttbn2GU/" target="_blank" rel="noopener" class="social-icon-btn" title="Facebook">
              <svg width="16" height="16" fill="currentColor" viewBox="0 0 24 24"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/></svg>
            </a>
            <a href="https://www.instagram.com/rizeworld?stkn=MWYxOGs5NGhhdnNsNA==" target="_blank" rel="noopener" class="social-icon-btn" title="Instagram">
              <svg width="16" height="16" fill="currentColor" viewBox="0 0 24 24"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zm0-2.163c-3.259 0-3.667.014-4.947.072-4.358.2-6.78 2.618-6.98 6.98-.059 1.281-.073 1.689-.073 4.948 0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98 1.281.058 1.689.072 4.948.072 3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98-1.281-.059-1.69-.073-4.949-.073zm0 5.838c-3.403 0-6.162 2.759-6.162 6.162s2.759 6.163 6.162 6.163 6.162-2.759 6.162-6.163c0-3.403-2.759-6.162-6.162-6.162zm0 10.162c-2.209 0-4-1.79-4-4 0-2.209 1.791-4 4-4s4 1.791 4 4c0 2.21-1.791 4-4 4zm6.406-11.845c-.796 0-1.441.645-1.441 1.44s.645 1.44 1.441 1.44c.795 0 1.439-.645 1.439-1.44s-.644-1.44-1.439-1.44z"/></svg>
            </a>
            <a href="https://www.linkedin.com/company/rizeworld/" target="_blank" rel="noopener" class="social-icon-btn" title="LinkedIn">
              <svg width="15" height="15" fill="currentColor" viewBox="0 0 24 24"><path d="M19 0h-14c-2.761 0-5 2.239-5 5v14c0 2.761 2.239 5 5 5h14c2.762 0 5-2.239 5-5v-14c0-2.761-2.238-5-5-5zm-11 19h-3v-11h3v11zm-1.5-12.268c-.966 0-1.75-.79-1.75-1.764s.784-1.764 1.75-1.764 1.75.79 1.75 1.764-.783 1.764-1.75 1.764zm13.5 12.268h-3v-5.604c0-3.368-4-3.113-4 0v5.604h-3v-11h3v1.765c1.396-2.586 7-2.777 7 2.476v6.759z"/></svg>
            </a>
          </div>
        </div>
      </div>
    </div>

    <!-- Bottom Bar (Brand, Copyright, Legal) -->
    <div class="footer-bottom-bar">
      <div class="footer-bottom-container">
        <div class="footer-bottom-logo">
          <a href="https://rizemetricstool.com/" style="text-decoration:none;">
            <span class="brand-title" style="color:#ffffff; font-size:1.15rem; letter-spacing:-0.01em;">RIZEMETRICS</span>
          </a>
        </div>
        <div class="footer-bottom-copy">
          &copy; Rizeworld Digital Marketing Company. All rights reserved. RizeMetrics is an automated SEO intelligence and authority scoring platform.
        </div>
        <div class="footer-bottom-legal">
          <a href="/accessibility">Accessibility</a>
          <a href="/terms">Terms of Use</a>
          <a href="/privacy">Privacy Policy</a>
          <a href="/sitemap">Sitemap</a>
          <a href="/sitemap.xml">XML Sitemap</a>
        </div>
      </div>
    </div>
  </footer>

  <script>
    const CIRCUMFERENCE = 2 * Math.PI * 50; // r=50 -> 314.159

    function getScoreColor(score100) {
      if (score100 >= 50) return "#059669"; // Emerald
      if (score100 >= 32) return "#2563eb"; // Blue
      if (score100 >= 15) return "#0284c7"; // Cyan
      return "#ea580c"; // Warm Orange
    }

    function setGaugeValue(id, val) {
      const el = document.getElementById(id);
      if (!el) return;
      const str = String(val);
      el.textContent = str;
      const len = str.length;
      if (len >= 6) {
        el.style.fontSize = "1.35rem";
        el.style.letterSpacing = "-0.05em";
      } else if (len >= 5) {
        el.style.fontSize = "1.65rem";
        el.style.letterSpacing = "-0.04em";
      } else if (len >= 4) {
        el.style.fontSize = "1.85rem";
        el.style.letterSpacing = "-0.03em";
      } else {
        el.style.fontSize = "2.05rem";
        el.style.letterSpacing = "-0.02em";
      }
    }

    function setRadialGauge(circleEl, score100, customColor) {
      const clamped = Math.max(0, Math.min(100, score100));
      const offset = CIRCUMFERENCE - (clamped / 100) * CIRCUMFERENCE;
      circleEl.style.strokeDasharray = `${CIRCUMFERENCE}`;
      circleEl.style.strokeDashoffset = `${offset}`;
      circleEl.style.stroke = customColor || getScoreColor(clamped);
    }

    function updateCapsuleActive(activeId) {
      document.querySelectorAll('.capsule-navbar .capsule-item').forEach(b => b.classList.remove('active'));
      const activeBtn = document.getElementById(activeId);
      if (activeBtn) activeBtn.classList.add('active');
    }

    function openAuthorityTab() {
      updateCapsuleActive('navBtnAuthority');
      const res = document.getElementById('resultsSection');
      if (res && res.style.display !== 'none') {
        const btn = document.getElementById('btnTabPages');
        switchTab('tabPages', btn);
        const target = document.getElementById('tabPages');
        if (target) {
          target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      } else {
        const input = document.getElementById('urlInput');
        if (input) {
          input.focus();
          input.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      }
    }

    function openBacklinksTab() {
      updateCapsuleActive('navBtnBacklinks');
      const res = document.getElementById('resultsSection');
      if (res && res.style.display === 'none') {
        const input = document.getElementById('urlInput');
        if (input) {
          input.focus();
          input.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        return;
      }
      const btn = document.getElementById('btnTabInbound');
      switchTab('tabInbound', btn);
      const target = document.getElementById('tabInbound');
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }

    function openSpamTab() {
      updateCapsuleActive('navBtnSpam');
      const res = document.getElementById('resultsSection');
      if (res && res.style.display === 'none') {
        const input = document.getElementById('urlInput');
        if (input) {
          input.focus();
          input.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        return;
      }
      const btn = document.getElementById('tabSpamBtn');
      switchTab('tabSpam', btn);
      const target = document.getElementById('tabSpam');
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }

    function switchTab(panelId, btnEl) {
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      const targetPanel = document.getElementById(panelId);
      if (targetPanel) targetPanel.classList.add('active');
      if (btnEl) btnEl.classList.add('active');

      if (panelId === 'tabPages') {
        updateCapsuleActive('navBtnAuthority');
      } else if (panelId === 'tabInbound') {
        updateCapsuleActive('navBtnBacklinks');
      } else if (panelId === 'tabSpam') {
        updateCapsuleActive('navBtnSpam');
      } else if (panelId === 'tabMath') {
        updateCapsuleActive('navBtnMath');
      }
    }

    function escapeHtml(str) {
      if (str === null || str === undefined) return "";
      return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    window._lastBulkResults = [];

    function resetView() {
      const input = document.getElementById('urlInput');
      if (input) {
        input.value = '';
        input.focus();
      }
      document.getElementById('resultsSection').style.display = 'none';
      const bulkSec = document.getElementById('bulkResultsSection');
      if (bulkSec) bulkSec.style.display = 'none';
      const errBanner = document.getElementById('errorBanner');
      if (errBanner) errBanner.style.display = 'none';
      const ovSection = document.getElementById('authorityOverviewSection');
      if (ovSection) ovSection.style.display = 'block';
    }

    function runQuickPreset(url) {
      document.getElementById('urlInput').value = url;
      executeCheck(url);
    }

    const BULK_STOPWORDS = new Set([
      'url', 'urls', 'domain', 'domains', 'website', 'websites',
      'link', 'links', 'site', 'sites', 'page', 'pages', 'target',
      'address', 'host', 'hostname', 'da', 'pa', 'spam', 'score'
    ]);

    function parseBulkUrlTokens(rawText) {
      if (!rawText || typeof rawText !== 'string') return [];
      const parts = rawText.split(/[\r\n,;|\t\s]+/);
      const tokens = [];
      for (const p of parts) {
        const cleaned = p.trim().replace(/^["'`<>()\[\]{}]+|["'`<>()\[\]{}]+$/g, '');
        if (!cleaned) continue;
        if (BULK_STOPWORDS.has(cleaned.toLowerCase())) continue;
        if (cleaned.indexOf('.') === -1 || cleaned.length < 4) continue;
        tokens.push(cleaned);
      }
      return tokens;
    }

    function handleCheckSubmit(e) {
      e.preventDefault();
      const rawInput = document.getElementById('urlInput').value.trim();
      if (!rawInput) {
        document.getElementById('urlInput').focus();
        return;
      }
      const tokens = parseBulkUrlTokens(rawInput);
      if (tokens.length > 1) {
        executeBulkCheck(tokens);
      } else {
        executeCheck(tokens.length === 1 ? tokens[0] : rawInput);
      }
    }

    function handleCsvFileUpload(event) {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      const errBanner = document.getElementById('errorBanner');
      if (errBanner) errBanner.style.display = 'none';

      const reader = new FileReader();
      reader.onload = function(loadEvt) {
        const content = String(loadEvt.target.result || '');
        const tokens = parseBulkUrlTokens(content);
        if (tokens.length === 0) {
          if (errBanner) {
            errBanner.textContent = 'CSV Upload Error: No valid URLs or domain names were found in the uploaded file.';
            errBanner.style.display = 'block';
          }
          return;
        }
        const inputEl = document.getElementById('urlInput');
        if (inputEl) {
          inputEl.value = tokens.slice(0, 5).join(', ') + (tokens.length > 5 ? ` (+${tokens.length - 5} more)` : '');
        }
        executeBulkCheck(tokens);
      };
      reader.onerror = function() {
        if (errBanner) {
          errBanner.textContent = 'CSV Upload Error: Unable to read the selected file.';
          errBanner.style.display = 'block';
        }
      };
      reader.readAsText(file);
      event.target.value = '';
    }

    async function executeBulkCheck(urlList) {
      if (!Array.isArray(urlList) || urlList.length === 0) return;

      const btn = document.getElementById('checkBtn');
      const csvBtn = document.getElementById('uploadCsvBtn');
      const loading = document.getElementById('loadingBanner');
      const errBanner = document.getElementById('errorBanner');
      const liveCrawl = document.getElementById('optLiveCrawl').checked;
      const archive = document.getElementById('optArchive').checked;

      btn.disabled = true;
      if (csvBtn) csvBtn.disabled = true;
      errBanner.style.display = 'none';
      document.getElementById('resultsSection').style.display = 'none';

      safeSetText('loadingStepTitle', `Analyzing ${urlList.length} Bulk URLs (DA, PA & Spam Score)...`);
      safeSetText('loadingStepSub', 'Deduplicating URLs, computing Domain/Page Authority & 27-Signal Spam Score, and saving unique URLs to MongoDB...');
      loading.classList.add('active');

      try {
        const resp = await fetch('/api/bulk-check', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            urls: urlList,
            enable_live_crawl: liveCrawl,
            enable_archive_lookup: archive
          })
        });

        const data = await resp.json();
        if (!resp.ok || data.status === 'FAILED') {
          throw new Error(data.error_message || `HTTP Error ${resp.status}`);
        }

        renderBulkReport(data);
        loadHistory();
      } catch (err) {
        errBanner.textContent = `Bulk Evaluation Error: ${err.message}`;
        errBanner.style.display = 'block';
      } finally {
        btn.disabled = false;
        if (csvBtn) csvBtn.disabled = false;
        loading.classList.remove('active');
        safeSetText('loadingStepTitle', 'Analyzing URL & Building Web Graph...');
        safeSetText('loadingStepSub', 'Extracting internal/external links, querying Wayback CDX & solving PageRank...');
      }
    }

    function renderBulkReport(data) {
      const results = data.results || [];
      window._lastBulkResults = results;

      const singleSection = document.getElementById('resultsSection');
      if (singleSection) singleSection.style.display = 'none';
      const ovSection = document.getElementById('authorityOverviewSection');
      if (ovSection) ovSection.style.display = 'none';

      const bulkSection = document.getElementById('bulkResultsSection');
      if (bulkSection) bulkSection.style.display = 'block';

      safeSetText('bulkStatUnique', String(data.unique_evaluated || results.length));
      safeSetText('bulkStatDuplicates', String(data.duplicates_skipped || 0));

      let sumDa = 0, sumPa = 0, sumSpam = 0;
      results.forEach(r => {
        sumDa += Number(r.lp_da_display || 0);
        sumPa += Number(r.target_pa_display || 0);
        sumSpam += Number(r.moz_spam_score_pct || 1);
      });
      const count = Math.max(1, results.length);
      safeSetText('bulkStatAvgDa', (sumDa / count).toFixed(1));
      safeSetText('bulkStatAvgPa', (sumPa / count).toFixed(1));
      safeSetText('bulkStatAvgSpam', `${(sumSpam / count).toFixed(1)}%`);

      const tbody = document.getElementById('bulkResultsTableBody');
      if (!tbody) return;

      tbody.innerHTML = results.map((item, idx) => {
        const da = Number(item.lp_da_display || 0).toFixed(1);
        const pa = Number(item.target_pa_display || 0).toFixed(1);
        const spamPct = Number(item.moz_spam_score_pct || 1).toFixed(1);
        const risk = item.moz_spam_risk_level || 'Low';
        let spamClass = 'bulk-pill-spam-low';
        if (risk === 'High') spamClass = 'bulk-pill-spam-high';
        else if (risk === 'Medium') spamClass = 'bulk-pill-spam-med';

        const targetUrl = item.target_url || item.input_url || '';
        const domainName = item.domain_name || '';

        return `
          <tr>
            <td style="font-weight: 700; color: #64748b;">${item.rank || (idx + 1)}</td>
            <td>
              <div style="font-weight: 700; color: #0f172a; word-break: break-all;">
                <a href="${escapeHtml(targetUrl)}" target="_blank" rel="noopener noreferrer" style="color: #0f172a; text-decoration: none;">
                  ${escapeHtml(targetUrl)}
                </a>
              </div>
              <div style="font-size: 0.78rem; color: #64748b; margin-top: 0.15rem;">
                Domain: <strong>${escapeHtml(domainName)}</strong>
              </div>
            </td>
            <td style="text-align: center;">
              <span class="bulk-metric-pill bulk-pill-da">${da}</span>
            </td>
            <td style="text-align: center;">
              <span class="bulk-metric-pill bulk-pill-pa">${pa}</span>
            </td>
            <td style="text-align: center;">
              <span class="bulk-metric-pill ${spamClass}">${spamPct}% (${escapeHtml(risk)})</span>
            </td>
          </tr>
        `;
      }).join('');
    }

    function exportBulkResultsCsv() {
      const rows = window._lastBulkResults || [];
      if (!rows.length) return;
      const headers = ['Rank', 'URL', 'Domain', 'Domain Authority (DA)', 'Page Authority (PA)', 'Spam Score (%)', 'Spam Risk Level'];
      const csvLines = [headers.join(',')];
      rows.forEach((r, idx) => {
        const line = [
          r.rank || (idx + 1),
          `"${String(r.target_url || '').replace(/"/g, '""')}"`,
          `"${String(r.domain_name || '').replace(/"/g, '""')}"`,
          Number(r.lp_da_display || 0).toFixed(1),
          Number(r.target_pa_display || 0).toFixed(1),
          Number(r.moz_spam_score_pct || 1).toFixed(1),
          `"${String(r.moz_spam_risk_level || 'Low').replace(/"/g, '""')}"`
        ];
        csvLines.push(line.join(','));
      });
      const blob = new Blob([csvLines.join('\n')], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `rizemetrics-bulk-authority-${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }

    async function executeCheck(url) {
      if (!url || !url.trim()) {
        document.getElementById('urlInput').focus();
        return;
      }

      const btn = document.getElementById('checkBtn');
      const loading = document.getElementById('loadingBanner');
      const errBanner = document.getElementById('errorBanner');
      const liveCrawl = document.getElementById('optLiveCrawl').checked;
      const archive = document.getElementById('optArchive').checked;
      const maxPages = parseInt(document.getElementById('optMaxPages').value || "5", 10);

      btn.disabled = true;
      errBanner.style.display = "none";
      loading.classList.add('active');

      try {
        const resp = await fetch('/api/check', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            url: url,
            max_internal_pages: maxPages,
            enable_live_crawl: liveCrawl,
            enable_archive_lookup: archive
          })
        });

        const data = await resp.json();
        if (!resp.ok || data.status === "FAILED") {
          throw new Error(data.error_message || `HTTP Error ${resp.status}`);
        }

        try {
          renderReport(data);
        } catch (renderErr) {
          console.error("renderReport error:", renderErr);
        }
        loadHistory();
      } catch (err) {
        errBanner.textContent = `Evaluation Error: ${err.message}`;
        errBanner.style.display = "block";
      } finally {
        btn.disabled = false;
        loading.classList.remove('active');
      }
    }

    function safeSetText(id, text) {
      const el = document.getElementById(id);
      if (el) el.textContent = (text !== undefined && text !== null) ? text : '';
    }

    function renderReport(r) {
      const bulkSec = document.getElementById('bulkResultsSection');
      if (bulkSec) bulkSec.style.display = "none";
      const resSection = document.getElementById('resultsSection');
      if (resSection) resSection.style.display = "block";
      const ovSection = document.getElementById('authorityOverviewSection');
      if (ovSection) ovSection.style.display = "none";

      // Card 1: Domain Authority (Warm Pastel Peach)
      safeSetText('daDomainLabel', r.domain_name || '');
      setGaugeValue('daScoreNum', (r.lp_da_display !== undefined ? r.lp_da_display : 0).toFixed(1));
      setRadialGauge(document.getElementById('daGaugeCircle'), r.lp_da_display || 0, "#ea580c");

      safeSetText('daTierBadge', (r.authority_tier || 'Standard').replace(' / Niche Authority', ' Authority'));
      safeSetText('daAnchorVal', `${(r.anchor_page_display || 0).toFixed(1)} / 100`);
      const anchorBar = document.getElementById('daAnchorBar');
      if (anchorBar) anchorBar.style.width = `${Math.min(100, r.anchor_page_display || 0)}%`;

      safeSetText('daTopKVal', `${(r.top_k_harmonic_display || 0).toFixed(1)} / 100`);
      const topKBar = document.getElementById('daTopKBar');
      if (topKBar) topKBar.style.width = `${Math.min(100, r.top_k_harmonic_display || 0)}%`;

      safeSetText('daBreadthVal', `${(r.breadth_display || 0).toFixed(1)} / 100`);
      const breadthBar = document.getElementById('daBreadthBar');
      if (breadthBar) breadthBar.style.width = `${Math.min(100, r.breadth_display || 0)}%`;

      safeSetText('daMeanVal', `${(r.mean_page_display || 0).toFixed(1)} / 100`);
      const meanBar = document.getElementById('daMeanBar');
      if (meanBar) meanBar.style.width = `${Math.min(100, r.mean_page_display || 0)}%`;

      safeSetText('daNormalizedLabel', `LP-DA: ${(r.lp_da_score || 0).toFixed(6)}`);
      safeSetText('daScoredPagesLabel', `Scored Pages: ${r.scored_pages_count || 0}`);

      // Card 2: Page Authority (Pastel Sky Blue)
      safeSetText('paUrlLabel', r.target_url || '');
      setGaugeValue('paScoreNum', (r.target_pa_display !== undefined ? r.target_pa_display : 0).toFixed(1));
      setRadialGauge(document.getElementById('paGaugeCircle'), r.target_pa_display || 0, "#2563eb");
      safeSetText('paRankBadge', `Rank #${r.target_page_rank_on_domain || 1} of ${r.scored_pages_count || 1}`);

      safeSetText('paRawPrVal', Number(r.target_raw_pagerank || 0).toExponential(3));
      safeSetText('paCompScaleVal', (r.target_comp_scale || 0).toFixed(3));
      safeSetText('paWccSizeVal', `${r.target_component_nodes || 0} nodes`);
      safeSetText('paIterVal', `${r.pagerank_iterations || 0} iters`);
      safeSetText('paNormalizedLabel', `LP-PA: ${(r.target_pa_score || 0).toFixed(6)}`);

      // Card 3: Moz Spam Score (Pastel Mint Green)
      const spamPctNum = (r.moz_spam_score_pct !== undefined) ? r.moz_spam_score_pct : 1.0;
      const spamRiskLvl = r.moz_spam_risk_level || "Low";
      const spamHex = spamRiskLvl === "Low" ? "#059669" : (spamRiskLvl === "Medium" ? "#d97706" : "#dc2626");

      setGaugeValue('spamScoreNum', `${spamPctNum.toFixed(0)}%`);
      setRadialGauge(document.getElementById('spamGaugeCircle'), spamPctNum, spamHex);

      const spamCardBadge = document.getElementById('spamCardRiskBadge');
      if (spamCardBadge) {
        spamCardBadge.textContent = `${spamRiskLvl} Risk`;
        spamCardBadge.style.color = spamHex;
      }

      const flaggedCnt = r.moz_spam_signals_triggered_count || 0;
      const checkedCnt = r.moz_spam_signals_checked || 27;
      safeSetText('spamFlaggedCountVal', `${flaggedCnt} / ${checkedCnt}`);
      const fBar = document.getElementById('spamFlaggedBar');
      if (fBar) {
        fBar.style.width = `${Math.min(100, (flaggedCnt / Math.max(1, checkedCnt)) * 100)}%`;
        fBar.style.background = spamHex;
      }

      safeSetText('spamMozTrustVal', `${(r.moz_trust_score || 0).toFixed(1)} / 10`);
      safeSetText('spamMozRankVal', `${(r.moz_rank_score || 0).toFixed(1)} / 10`);
      safeSetText('spamPenaltyVal', `${((r.moz_spam_penalty || 0) * 100).toFixed(1)}%`);

      // Card 4: Link Profile & Counts (Pastel Lilac)
      const srcList = (r.data_sources_used || []);
      const srcLabelEl = document.getElementById('linkSourcesLabel');
      if (srcLabelEl) {
        srcLabelEl.textContent = `${srcList.length} Live Data Sources`;
        srcLabelEl.title = `Sources: ${srcList.join(', ')}`;
      }
      safeSetText('execTimeBadge', `${(r.execution_time_ms || 0).toFixed(0)} ms`);
      safeSetText('statTotalLinks', (r.total_links_found || 0).toLocaleString());
      safeSetText('statInternalLinks', (r.internal_links_count || 0).toLocaleString());
      safeSetText('statInboundLinks', (r.inbound_backlinks_count || 0).toLocaleString());
      safeSetText('statExternalLinks', (r.external_outbound_links_count || 0).toLocaleString());

      const totalRel = Math.max(1, (r.dofollow_links_count || 0) + (r.nofollow_links_count || 0) + (r.ugc_links_count || 0) + (r.sponsored_links_count || 0));
      const dofollowPct = Math.round(((r.dofollow_links_count || 0) / totalRel) * 100);
      safeSetText('statRelRatioVal', `${dofollowPct}% (${r.dofollow_links_count || 0}/${totalRel})`);
      const relBar = document.getElementById('statRelBar');
      if (relBar) relBar.style.width = `${dofollowPct}%`;

      safeSetText('statRefDomains', `Ref Domains: ${r.referring_domains_count || 0}`);
      const archYr = r.earliest_archive_year ? ` (${r.earliest_archive_year})` : "";
      safeSetText('statWaybackSnaps', `Archive: ${r.wayback_snapshots_count || 0}${archYr}`);

      // Metadata Strip
      safeSetText('metaTitle', r.target_title || r.domain_name || '-');
      safeSetText('metaHttpStatus', `HTTP ${r.target_http_status || 200} • ${r.target_canonical_url || r.target_url || ''}`);
      safeSetText('metaH1', (r.target_h1_headings && r.target_h1_headings.length > 0) ? r.target_h1_headings[0] : "N/A");
      const trustVal = (r.moz_trust_score !== undefined) ? r.moz_trust_score.toFixed(1) : "-";
      const rankVal = (r.moz_rank_score !== undefined) ? r.moz_rank_score.toFixed(1) : "-";
      const spamPct = (r.moz_spam_score_pct !== undefined) ? r.moz_spam_score_pct.toFixed(1) : "1.0";
      const spamRisk = r.moz_spam_risk_level || "Low";
      safeSetText('metaGraphVE', `Trust: ${trustVal}/10 • Rank: ${rankVal}/10 • Spam: ${spamPct}% (${spamRisk})`);

      // Tab Counts
      const pages = r.top_constituent_pages || [];
      const inbound = r.inbound_backlinks || [];
      const internal = r.internal_links_sample || [];
      const external = r.external_links_sample || [];
      const spamAudit = r.moz_spam_audit || [];

      safeSetText('countPagesTab', pages.length);
      safeSetText('countInboundTab', inbound.length);
      safeSetText('countInternalTab', internal.length);
      safeSetText('countExternalTab', external.length);
      safeSetText('countSpamTab', r.moz_spam_signals_triggered_count || 0);

      // Render Tab 1: Constituent Pages
      const tbodyPages = document.getElementById('tbodyConstituentPages');
      if (tbodyPages) {
        tbodyPages.innerHTML = pages.length ? pages.map(p => `
          <tr class="${p.is_target_url ? 'target-highlight' : ''}">
            <td style="font-weight:700; color:var(--text-main);" class="mono">#${p.rank || 1}</td>
            <td class="url-cell">
              <a href="${escapeHtml(p.normalized_url)}" target="_blank" rel="noopener" class="url-link">${escapeHtml(p.normalized_url)}</a>
              ${p.is_target_url ? '<span class="tag tag-target">INSPECTED URL</span>' : ''}
              ${p.title ? `<div style="font-size:0.75rem; color:var(--text-soft);">${escapeHtml(p.title)}</div>` : ''}
            </td>
            <td>
              <div class="pa-bar-wrap">
                <span style="font-weight:700; width:42px; color:var(--text-main);" class="mono">${(p.lp_pa_display || 0).toFixed(2)}</span>
                <div class="pa-bar-bg"><div class="pa-bar-fill" style="width:${Math.min(100, p.lp_pa_display || 0)}%;"></div></div>
              </div>
            </td>
            <td class="mono">${(p.lp_pa_score || 0).toFixed(6)}</td>
            <td class="mono">${Number(p.raw_pagerank || 0).toExponential(4)}</td>
            <td class="mono">${p.inbound_links_count || 0} in / ${p.outbound_links_count || 0} out</td>
            <td><span class="tag tag-ctx">${escapeHtml(p.source_type || 'crawl')}</span></td>
            <td>
              <button type="button" class="preset-btn" onclick="runQuickPreset('${escapeHtml(p.normalized_url)}')">Check PA</button>
            </td>
          </tr>
        `).join('') : `<tr><td colspan="8" style="text-align:center; color:var(--text-soft); padding:1.5rem;">No constituent pages available.</td></tr>`;
      }

      // Render Tab 2: Inbound Backlinks & Referring Sites Breakdown
      safeSetText('inboundTotalBuiltVal', (r.inbound_backlinks_count || 0).toLocaleString());
      safeSetText('inboundTotalLrdVal', (r.referring_domains_count || 0).toLocaleString());

      const siteGroups = {};
      inbound.forEach(b => {
        const dom = b.source_domain || 'unknown';
        if (!siteGroups[dom]) {
          siteGroups[dom] = { domain: dom, count: 0, dofollow: 0, nofollow: 0, prov: b.provenance || 'index' };
        }
        siteGroups[dom].count += 1;
        if ((b.rel_attributes || '').includes('nofollow')) siteGroups[dom].nofollow += 1;
        else siteGroups[dom].dofollow += 1;
      });
      const groupedSites = Object.values(siteGroups).sort((a, b) => b.count - a.count);
      safeSetText('inboundSampleSitesText',
        groupedSites.length ? `Showing ${inbound.length} sampled backlinks across ${groupedSites.length} referring sites` : 'No sample URLs discovered');

      const tbodySites = document.getElementById('tbodyReferringSites');
      if (tbodySites) {
        tbodySites.innerHTML = groupedSites.length ? groupedSites.map(g => `
          <tr>
            <td style="font-weight:700; color:var(--text-main);">${escapeHtml(g.domain)}</td>
            <td style="font-weight:700; color:var(--text-main);" class="mono">${g.count}</td>
            <td class="mono">${g.dofollow} Dofollow / ${g.nofollow} Nofollow</td>
            <td><span class="tag tag-ctx">${escapeHtml(g.prov)}</span></td>
          </tr>
        `).join('') : `<tr><td colspan="4" style="text-align:center; color:var(--text-soft);">No referring sites sampled.</td></tr>`;
      }

      const tbodyInbound = document.getElementById('tbodyInboundLinks');
      if (tbodyInbound) {
        tbodyInbound.innerHTML = inbound.length ? inbound.map(b => `
          <tr>
            <td class="url-cell"><a href="${escapeHtml(b.source_url)}" target="_blank" rel="noopener" class="url-link">${escapeHtml(b.source_url)}</a></td>
            <td style="font-weight:600; color:var(--text-main);">${escapeHtml(b.source_domain)}</td>
            <td class="url-cell">${escapeHtml(b.target_url)}</td>
            <td>${escapeHtml(b.anchor_text || '[No Anchor]')}</td>
            <td><span class="tag ${(b.rel_attributes || '').includes('nofollow') ? 'tag-nofollow' : 'tag-dofollow'}">${escapeHtml(b.rel_attributes || '')}</span></td>
            <td><span class="tag tag-ctx">${escapeHtml(b.dom_context || '')}</span></td>
            <td><span class="tag tag-ctx">${escapeHtml(b.provenance || '')}</span></td>
          </tr>
        `).join('') : `<tr><td colspan="7" style="text-align:center; color:var(--text-soft);">No inbound backlinks recorded.</td></tr>`;
      }

      // Render Tab 3: Internal Links
      const tbodyInternal = document.getElementById('tbodyInternalLinks');
      if (tbodyInternal) {
        tbodyInternal.innerHTML = internal.length ? internal.map(l => `
          <tr>
            <td class="url-cell">${escapeHtml(l.source_url)}</td>
            <td class="url-cell"><a href="${escapeHtml(l.target_url)}" target="_blank" rel="noopener" class="url-link">${escapeHtml(l.target_url)}</a></td>
            <td>${escapeHtml(l.anchor_text || '-')}</td>
            <td><span class="tag ${(l.rel_attributes || '').includes('nofollow') ? 'tag-nofollow' : 'tag-dofollow'}">${escapeHtml(l.rel_attributes || '')}</span></td>
            <td><span class="tag tag-ctx">${escapeHtml(l.dom_context || '')}</span></td>
          </tr>
        `).join('') : `<tr><td colspan="5" style="text-align:center; color:var(--text-soft);">No internal links extracted on this page.</td></tr>`;
      }

      // Render Tab 4: External Links
      const tbodyExternal = document.getElementById('tbodyExternalLinks');
      if (tbodyExternal) {
        tbodyExternal.innerHTML = external.length ? external.map(l => `
          <tr>
            <td class="url-cell">${escapeHtml(l.source_url)}</td>
            <td class="url-cell"><a href="${escapeHtml(l.target_url)}" target="_blank" rel="noopener" class="url-link">${escapeHtml(l.target_url)}</a></td>
            <td style="color:#7c3aed; font-weight:600;">${escapeHtml(l.target_domain)}</td>
            <td>${escapeHtml(l.anchor_text || '-')}</td>
            <td><span class="tag ${(l.rel_attributes || '').includes('nofollow') ? 'tag-nofollow' : 'tag-dofollow'}">${escapeHtml(l.rel_attributes || '')}</span></td>
            <td><span class="tag tag-ctx">${escapeHtml(l.dom_context || '')}</span></td>
          </tr>
        `).join('') : `<tr><td colspan="6" style="text-align:center; color:var(--text-soft);">No external outbound links extracted.</td></tr>`;
      }

      // Render Tab 5: Spam Score 27-Signal Audit
      const spamBadge = document.getElementById('spamSummaryBadge');
      if (spamBadge) {
        const spamColor = spamRisk === "Low" ? "#059669" : (spamRisk === "Medium" ? "#d97706" : "#dc2626");
        const summaryBg = spamRisk === "Low" ? "#ecfdf5" : (spamRisk === "Medium" ? "#fffbeb" : "#fef2f2");
        const summaryBorder = spamRisk === "Low" ? "#a7f3d0" : (spamRisk === "Medium" ? "#fde68a" : "#fecaca");
        spamBadge.textContent = `Spam Score: ${spamPct}% (${spamRisk} Risk) • ${r.moz_spam_signals_triggered_count || 0}/27 Flagged`;
        spamBadge.style.background = summaryBg;
        spamBadge.style.color = spamColor;
        spamBadge.style.border = `1px solid ${summaryBorder}`;
      }

      const tbodySpam = document.getElementById('tbodySpamAudit');
      if (tbodySpam) {
        tbodySpam.innerHTML = spamAudit.length ? spamAudit.map(sig => {
          let statusStyle = "";
          if (sig.status === "FLAGGED") {
            statusStyle = "background:#fef2f2; color:#dc2626; border:1px solid #fecaca;";
          } else if (sig.status === "PASS") {
            statusStyle = "background:#ecfdf5; color:#059669; border:1px solid #a7f3d0;";
          }
          return `
            <tr>
              <td style="font-weight:700; color:var(--text-main);" class="mono">${escapeHtml(sig.id)}</td>
              <td style="font-weight:600; color:var(--text-main);">${escapeHtml(sig.name)}</td>
              <td><span class="tag tag-ctx">${escapeHtml(sig.category)}</span></td>
              <td><span class="tag" style="${statusStyle}">${escapeHtml(sig.status)}</span></td>
              <td style="color:var(--text-soft); font-size:0.8rem;">${escapeHtml(sig.detail)}</td>
            </tr>
          `;
        }).join('') : `<tr><td colspan="5" style="text-align:center; color:var(--text-soft);">No spam signals recorded.</td></tr>`;
      }

      // Render Tab 6: Mathematical Audit Box
      const mathBox = document.getElementById('mathAuditBox');
      if (mathBox) {
        const t1 = ((r.da_alpha || 0) * (r.anchor_page_pa || 0)).toFixed(6);
        const t2 = ((r.da_beta || 0) * (r.top_k_harmonic_pa || 0)).toFixed(6);
        const t3 = ((r.da_gamma || 0) * (r.breadth_score || 0) * (r.mean_page_pa || 0)).toFixed(6);
        mathBox.innerHTML = `
          <div style="color:var(--text-main); margin-bottom:4px;"><strong>1. Domain Authority Model (${escapeHtml(r.algorithm_version || 'LP-DA v1.0.0')}):</strong></div>
          <div>   LP-DA(D) = &alpha; &middot; A(D) + &beta; &middot; T<sub>k</sub>(D) + &gamma; &middot; B(D) &middot; M(D)</div>
          <div>   LP-DA(${escapeHtml(r.domain_name || '')}) = (${r.da_alpha} &times; ${(r.anchor_page_pa || 0).toFixed(6)}) + (${r.da_beta} &times; ${(r.top_k_harmonic_pa || 0).toFixed(6)}) + (${r.da_gamma} &times; ${(r.breadth_score || 0).toFixed(6)} &times; ${(r.mean_page_pa || 0).toFixed(6)})</div>
          <div>   LP-DA(${escapeHtml(r.domain_name || '')}) = ${t1} + ${t2} + ${t3} = <strong style="color:var(--text-main);">${(r.lp_da_score || 0).toFixed(6)}</strong> &rarr; Display: <strong style="color:var(--primary);">${(r.lp_da_display || 0).toFixed(2)} / 100</strong></div>
          <div style="margin-top:0.6rem; color:#059669;">   &check; Level-1 Reconstruction Audit: ${(r.level1_reconstructed_da || 0).toFixed(6)} (Exact Match: ${r.level1_audit_passed ? 'PASSED' : 'FAILED'})</div>
          <br/>
          <div style="color:var(--text-main); margin-bottom:4px;"><strong>2. Target Page Authority Model (LP-PA):</strong></div>
          <div>   Target URL: ${escapeHtml(r.target_url || '')}</div>
          <div>   Stationary PageRank (p<sub>i</sub>): ${Number(r.target_raw_pagerank || 0).toExponential(6)} | Component Scale: ${(r.target_comp_scale || 0).toFixed(6)} (${r.target_component_nodes || 0} vertices)</div>
          <div>   LP-PA(u) = min(1.0, log<sub>10</sub>(1 + p<sub>directed</sub> &middot; CompScale &middot; 100) / log<sub>10</sub>(51)) = <strong style="color:var(--text-main);">${(r.target_pa_score || 0).toFixed(6)}</strong> &rarr; Display: <strong style="color:var(--primary);">${(r.target_pa_display || 0).toFixed(2)} / 100</strong></div>
        `;
      }
    }

    async function loadHistory() {
      try {
        const resp = await fetch('/api/history');
        const data = await resp.json();
        const items = data.history || [];
        const section = document.getElementById('historySection');
        const grid = document.getElementById('historyGrid');
        if (items.length === 0) {
          section.style.display = "none";
          return;
        }
        section.style.display = "block";
        grid.innerHTML = items.map(item => `
          <div class="history-item" onclick="runQuickPreset('${escapeHtml(item.target_url)}')">
            <div style="overflow:hidden;">
              <div style="font-weight:700; font-size:0.86rem; color:var(--text-main);">${escapeHtml(item.domain_name)}</div>
              <div style="font-size:0.72rem; color:var(--text-soft); text-overflow:ellipsis; overflow:hidden; white-space:nowrap;">${escapeHtml(item.target_url)}</div>
            </div>
            <div style="text-align:right; flex-shrink:0; margin-left:0.75rem;">
              <div style="font-weight:800; color:var(--primary); font-size:0.92rem;" class="mono">DA ${item.lp_da_display.toFixed(1)}</div>
              <div style="font-weight:700; color:#0284c7; font-size:0.78rem;" class="mono">PA ${item.target_pa_display.toFixed(1)}</div>
            </div>
          </div>
        `).join('');
      } catch (e) {
        // Ignore history fetch errors
      }
    }

    // Load history on startup; search bar is empty by default until user enters a link
    window.addEventListener('DOMContentLoaded', () => {
      loadHistory();
      const input = document.getElementById('urlInput');
      const val = input ? input.value.trim() : '';
      if (val) {
        executeCheck(val);
      }
    });
  </script>
</body>
</html>
"""


# ==============================================================================
# UNIFIED REQUEST DISPATCHER & HTTP SERVER
# ==============================================================================

def handle_web_request(
    method: str,
    raw_path: str,
    raw_body: str = "",
    db_path: str = "./data/linkpilot.db",
) -> Tuple[int, str, Any]:
    """
    Core HTTP route dispatcher for LinkPilot Web UI and REST API.
    Returns (http_status_code, content_type, payload_str_or_dict).
    """
    parsed = urlsplit(raw_path)
    path = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query)
    method_upper = method.upper()

    if method_upper == "GET":
        if path in ("/", "/index.html"):
            return int(HTTPStatus.OK), "text/html; charset=utf-8", DASHBOARD_HTML

        if path in ("/sitemap.xml", "/sitemap_index.xml"):
            base_url = os.environ.get("SITE_BASE_URL", "https://rizemetricstool.com").rstrip("/")
            sitemap_entries = [
                ("/", "daily", "1.0"),
                ("/spam-guide", "weekly", "0.9"),
                ("/seo-learning-center", "weekly", "0.9"),
                ("/beginners-guide", "weekly", "0.8"),
                ("/how-to-guides", "weekly", "0.8"),
                ("/blog", "weekly", "0.8"),
                ("/about", "monthly", "0.7"),
                ("/contact", "monthly", "0.7"),
                ("/sitemap", "monthly", "0.6"),
                ("/accessibility", "monthly", "0.5"),
                ("/terms", "monthly", "0.5"),
                ("/privacy", "monthly", "0.5"),
            ]
            urls_xml = "\n".join(
                f"  <url>\n"
                f"    <loc>{base_url}{route if route != '/' else '/'}</loc>\n"
                f"    <lastmod>2026-11-26</lastmod>\n"
                f"    <changefreq>{freq}</changefreq>\n"
                f"    <priority>{prio}</priority>\n"
                f"  </url>"
                for route, freq, prio in sitemap_entries
            )
            xml_payload = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                f"{urls_xml}\n"
                "</urlset>\n"
            )
            return int(HTTPStatus.OK), "application/xml; charset=utf-8", xml_payload

        if path == "/robots.txt":
            base_url = os.environ.get("SITE_BASE_URL", "https://rizemetricstool.com").rstrip("/")
            robots_txt = (
                "User-agent: *\n"
                "Allow: /\n"
                "Disallow: /api/\n\n"
                f"Sitemap: {base_url}/sitemap.xml\n"
            )
            return int(HTTPStatus.OK), "text/plain; charset=utf-8", robots_txt

        if is_footer_page(path):
            html_page = get_footer_page_html(path)
            if html_page is not None:
                return int(HTTPStatus.OK), "text/html; charset=utf-8", html_page

        if path in ("/favicon.ico", "/favicon.png", "/logo.png", "/logo.jpg", "/logo.webp", "/og-image.jpg"):
            logo_file = os.path.join(os.path.dirname(__file__), "logo.png")
            if os.path.exists(logo_file):
                with open(logo_file, "rb") as lf:
                    return int(HTTPStatus.OK), "image/png", lf.read()

        if path == "/api/health":
            return int(HTTPStatus.OK), "application/json; charset=utf-8", {
                "status": "ok",
                "service": "LinkPilot AI On-Demand DA/PA Engine",
                "algorithm_version": "LP-DA-v1.0.0",
                "db_exists": os.path.exists(db_path),
            }

        if path == "/api/domains":
            domains = get_known_db_domains(db_path)
            return int(HTTPStatus.OK), "application/json; charset=utf-8", {
                "domains": domains,
                "count": len(domains),
            }

        if path == "/api/history":
            hist = get_recent_history()
            return int(HTTPStatus.OK), "application/json; charset=utf-8", {
                "history": hist,
                "count": len(hist),
            }

        if path == "/api/check":
            raw_url = (query.get("url") or [""])[0].strip()
            if not raw_url:
                return int(HTTPStatus.BAD_REQUEST), "application/json; charset=utf-8", {
                    "status": "FAILED",
                    "error_message": "Missing required query parameter: 'url'",
                }

            max_pages = int((query.get("max_pages") or ["5"])[0])
            live_crawl = (query.get("live_crawl") or ["true"])[0].lower() not in ("false", "0", "no")
            archive = (query.get("archive") or ["true"])[0].lower() not in ("false", "0", "no")

            analyzer = OnDemandAuthorityAnalyzer(
                db_path=db_path,
                max_internal_pages=max_pages,
                enable_live_crawl=live_crawl,
                enable_archive_lookup=archive,
            )
            report = analyzer.analyze(raw_url)
            record_check_in_history(report)
            code = int(HTTPStatus.OK) if report.status == "COMPLETED" else int(HTTPStatus.BAD_REQUEST)
            return code, "application/json; charset=utf-8", report.to_dict()

        return int(HTTPStatus.NOT_FOUND), "application/json; charset=utf-8", {
            "status": "FAILED",
            "error_message": f"Not found: {path}",
        }

    if method_upper == "POST":
        if path == "/api/check":
            try:
                body = json.loads(raw_body or "{}")
            except json.JSONDecodeError:
                return int(HTTPStatus.BAD_REQUEST), "application/json; charset=utf-8", {
                    "status": "FAILED",
                    "error_message": "Invalid JSON request body.",
                }

            raw_url = str(body.get("url") or "").strip()
            if not raw_url:
                return int(HTTPStatus.BAD_REQUEST), "application/json; charset=utf-8", {
                    "status": "FAILED",
                    "error_message": "Please provide a valid URL or domain name.",
                }

            max_pages = int(body.get("max_internal_pages", 5))
            live_crawl = bool(body.get("enable_live_crawl", True))
            archive = bool(body.get("enable_archive_lookup", True))

            analyzer = OnDemandAuthorityAnalyzer(
                db_path=db_path,
                max_internal_pages=max_pages,
                enable_live_crawl=live_crawl,
                enable_archive_lookup=archive,
            )
            report = analyzer.analyze(raw_url)
            record_check_in_history(report)
            code = int(HTTPStatus.OK) if report.status == "COMPLETED" else int(HTTPStatus.BAD_REQUEST)
            return code, "application/json; charset=utf-8", report.to_dict()

        if path == "/api/bulk-check":
            try:
                body = json.loads(raw_body or "{}")
            except json.JSONDecodeError:
                return int(HTTPStatus.BAD_REQUEST), "application/json; charset=utf-8", {
                    "status": "FAILED",
                    "error_message": "Invalid JSON request body.",
                }

            raw_urls = body.get("urls")
            if isinstance(raw_urls, str):
                raw_urls_list = [raw_urls]
            elif isinstance(raw_urls, list):
                raw_urls_list = [str(u) for u in raw_urls if u is not None]
            else:
                raw_urls_list = []

            if not raw_urls_list:
                return int(HTTPStatus.BAD_REQUEST), "application/json; charset=utf-8", {
                    "status": "FAILED",
                    "error_message": "Please provide a list of URLs or domains to check.",
                }

            live_crawl = bool(body.get("enable_live_crawl", True))
            archive = bool(body.get("enable_archive_lookup", False))

            bulk_payload = evaluate_bulk_urls(
                raw_urls=raw_urls_list,
                db_path=db_path,
                enable_live_crawl=live_crawl,
                enable_archive_lookup=archive,
            )
            code = int(HTTPStatus.OK) if bulk_payload.get("status") == "COMPLETED" else int(HTTPStatus.BAD_REQUEST)
            return code, "application/json; charset=utf-8", bulk_payload

        return int(HTTPStatus.NOT_FOUND), "application/json; charset=utf-8", {
            "status": "FAILED",
            "error_message": f"Not found: {path}",
        }

    return int(HTTPStatus.METHOD_NOT_ALLOWED), "application/json; charset=utf-8", {
        "status": "FAILED",
        "error_message": f"Method not allowed: {method_upper}",
    }


class LinkPilotWebHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler serving the LinkPilot DA/PA Web UI and JSON REST API."""

    db_path: str = "./data/linkpilot.db"

    def log_message(self, format: str, *args: Any) -> None:
        """Route HTTP access logs through loguru at debug level."""
        logger.debug(f"WebUI [{self.client_address[0]}] {format % args}")

    def _dispatch(self, method: str, raw_body: str = "") -> None:
        status_code, content_type, payload = handle_web_request(
            method=method,
            raw_path=self.path,
            raw_body=raw_body,
            db_path=self.db_path,
        )
        if isinstance(payload, bytes):
            body_bytes = payload
        elif isinstance(payload, str):
            body_bytes = payload.encode("utf-8")
        else:
            body_bytes = json.dumps(payload, default=str).encode("utf-8")

        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        content_len = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        self._dispatch("POST", raw_body=raw_body)


def create_web_server(
    host: str = "127.0.0.1",
    port: int = 8501,
    db_path: str = "./data/linkpilot.db",
) -> ThreadingHTTPServer:
    """Create and configure a ThreadingHTTPServer instance for the LinkPilot Web UI."""
    LinkPilotWebHandler.db_path = db_path
    server = ThreadingHTTPServer((host, port), LinkPilotWebHandler)
    return server


def run_web_server(
    host: str = "127.0.0.1",
    port: int = 8501,
    db_path: str = "./data/linkpilot.db",
) -> None:
    """Start the LinkPilot Interactive Web UI server and block until interrupted."""
    server = create_web_server(host=host, port=port, db_path=db_path)
    actual_host, actual_port = server.server_address
    logger.info(f"LinkPilot AI Web UI running at http://{actual_host}:{actual_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down LinkPilot AI Web UI server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    _host = os.environ.get("HOST", "0.0.0.0")
    _port = int(os.environ.get("PORT", "8501"))
    run_web_server(host=_host, port=_port)
