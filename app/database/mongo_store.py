"""
LinkPilot AI — Asynchronous MongoDB Persistence for On-Demand DA/PA & URL Discovery.
app/database/mongo_store.py

Stores:
  1. `domains` collection: Main checked website URL, root domain, DA, PA, Spam Score,
     MozTrust, MozRank, link counts, and full array of discovered page URLs.
  2. `pages` collection: Every individual discovered/crawled page URL on the site
     with its PA score, raw PageRank, inbound/outbound counts, and source type.
  3. `check_history` collection: Timestamped audit log of each check including
     sampled inbound backlinks, internal links, and external outbound links.

Strictly non-blocking and fault-tolerant: never mutates ./data/linkpilot.db and
never blocks or fails offline unit tests.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from loguru import logger

_MONGO_LOCK = threading.Lock()
_MONGO_CLIENT: Any = None
_MONGO_URI_CACHED: Optional[str] = None

DEFAULT_DB_NAME = "linkpilot_seo"


def get_configured_mongo_uri() -> Optional[str]:
    """
    Read MONGODB_URI from environment or local .env file.
    Returns None if unconfigured or if <db_username> placeholder is still unresolved.
    """
    uri = os.environ.get("MONGODB_URI", "").strip()
    if not uri and os.path.exists(".env"):
        try:
            with open(".env", "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("MONGODB_URI="):
                        uri = s.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except Exception:
            pass
    if not uri or "<db_username>" in uri or "<password>" in uri:
        return None
    return uri


def _get_mongo_db() -> Optional[Any]:
    global _MONGO_CLIENT, _MONGO_URI_CACHED
    uri = get_configured_mongo_uri()
    if not uri:
        return None
    with _MONGO_LOCK:
        if _MONGO_CLIENT is None or _MONGO_URI_CACHED != uri:
            try:
                from pymongo import MongoClient
                _MONGO_CLIENT = MongoClient(
                    uri,
                    serverSelectionTimeoutMS=4000,
                    connectTimeoutMS=4000,
                    socketTimeoutMS=5000,
                )
                _MONGO_URI_CACHED = uri
            except Exception as e:
                logger.debug(f"MongoDB client initialization skipped: {e}")
                return None
        db_name = os.environ.get("MONGODB_DB_NAME", DEFAULT_DB_NAME).strip() or DEFAULT_DB_NAME
        return _MONGO_CLIENT[db_name]


def save_report_to_mongodb_sync(report_dict: Dict[str, Any]) -> bool:
    """
    Persist an OnDemandAuthorityReport dictionary into MongoDB collections:
      - `domains`: Upsert by `domain_name` (stores main URL, all page URLs, DA, PA, Spam Score).
      - `pages`: Bulk upsert every discovered constituent page URL by `normalized_url`.
      - `check_history`: Insert timestamped check snapshot.
    Returns True if saved, False if skipped/unavailable.
    """
    if not report_dict or report_dict.get("status") != "COMPLETED":
        return False

    db = _get_mongo_db()
    if db is None:
        return False

    try:
        from pymongo import UpdateOne

        now_iso = datetime.now(timezone.utc).isoformat()
        domain_name = report_dict.get("domain_name") or ""
        target_url = report_dict.get("target_url") or ""
        root_url = report_dict.get("root_url") or ""
        if not domain_name:
            return False

        constituent_pages: List[Dict[str, Any]] = (
            report_dict.get("top_constituent_pages")
            or report_dict.get("constituent_pages")
            or []
        )
        internal_links: List[Dict[str, Any]] = report_dict.get("internal_links_sample") or []
        external_links: List[Dict[str, Any]] = report_dict.get("external_links_sample") or []
        inbound_backlinks: List[Dict[str, Any]] = report_dict.get("inbound_backlinks") or []

        # Collect all unique page URLs belonging to this website
        all_page_urls_set = {target_url, root_url}
        for p in constituent_pages:
            u = p.get("normalized_url")
            if u:
                all_page_urls_set.add(u)
        for lnk in internal_links:
            if lnk.get("source_url"):
                all_page_urls_set.add(lnk["source_url"])
            if lnk.get("target_url"):
                all_page_urls_set.add(lnk["target_url"])
        all_page_urls = sorted(u for u in all_page_urls_set if u)

        # 1. Upsert Main Domain Document in `domains`
        domain_doc = {
            "domain_name": domain_name,
            "main_url": root_url,
            "last_target_url": target_url,
            "lp_da_display": report_dict.get("lp_da_display"),
            "target_pa_display": report_dict.get("target_pa_display"),
            "authority_tier": report_dict.get("authority_tier"),
            "moz_spam_score_pct": report_dict.get("moz_spam_score_pct"),
            "moz_spam_risk_level": report_dict.get("moz_spam_risk_level"),
            "moz_spam_signals_triggered_count": report_dict.get("moz_spam_signals_triggered_count"),
            "moz_trust_score": report_dict.get("moz_trust_score"),
            "moz_rank_score": report_dict.get("moz_rank_score"),
            "inbound_backlinks_count": report_dict.get("inbound_backlinks_count"),
            "referring_domains_count": report_dict.get("referring_domains_count"),
            "internal_links_count": report_dict.get("internal_links_count"),
            "external_outbound_links_count": report_dict.get("external_outbound_links_count"),
            "scored_pages_count": report_dict.get("scored_pages_count"),
            "last_checked_at": now_iso,
        }
        db["domains"].update_one(
            {"domain_name": domain_name},
            {
                "$set": domain_doc,
                "$setOnInsert": {"first_checked_at": now_iso},
                "$addToSet": {"all_page_urls": {"$each": all_page_urls}},
                "$inc": {"total_checks_count": 1},
            },
            upsert=True,
        )

        # 2. Bulk Upsert All Individual Page URLs in `pages`
        page_ops = []
        seen_page_urls = set()
        for p in constituent_pages:
            p_url = p.get("normalized_url")
            if not p_url or p_url in seen_page_urls:
                continue
            seen_page_urls.add(p_url)
            page_ops.append(
                UpdateOne(
                    {"normalized_url": p_url},
                    {
                        "$set": {
                            "normalized_url": p_url,
                            "domain_name": domain_name,
                            "main_url": root_url,
                            "lp_pa_display": p.get("lp_pa_display"),
                            "raw_pagerank": p.get("raw_pagerank"),
                            "rank_on_domain": p.get("rank"),
                            "inbound_links_count": p.get("inbound_links_count"),
                            "outbound_links_count": p.get("outbound_links_count"),
                            "source_type": p.get("source_type"),
                            "title": p.get("title"),
                            "last_checked_at": now_iso,
                        },
                        "$setOnInsert": {"first_seen_at": now_iso},
                    },
                    upsert=True,
                )
            )
        for extra_url in all_page_urls:
            if extra_url not in seen_page_urls:
                seen_page_urls.add(extra_url)
                page_ops.append(
                    UpdateOne(
                        {"normalized_url": extra_url},
                        {
                            "$set": {
                                "normalized_url": extra_url,
                                "domain_name": domain_name,
                                "main_url": root_url,
                                "last_checked_at": now_iso,
                            },
                            "$setOnInsert": {
                                "first_seen_at": now_iso,
                                "source_type": "discovered_internal_link",
                            },
                        },
                        upsert=True,
                    )
                )
        if page_ops:
            db["pages"].bulk_write(page_ops, ordered=False)

        # 3. Insert Check Audit Snapshot in `check_history`
        db["check_history"].insert_one({
            "query_input": report_dict.get("query_input"),
            "target_url": target_url,
            "main_url": root_url,
            "domain_name": domain_name,
            "checked_at": now_iso,
            "lp_da_display": report_dict.get("lp_da_display"),
            "target_pa_display": report_dict.get("target_pa_display"),
            "moz_spam_score_pct": report_dict.get("moz_spam_score_pct"),
            "moz_spam_risk_level": report_dict.get("moz_spam_risk_level"),
            "all_page_urls": all_page_urls,
            "inbound_backlinks_sample": inbound_backlinks[:50],
            "external_links_sample": external_links[:50],
        })

        logger.info(
            f"MongoDB persisted {domain_name} (main={root_url}, {len(all_page_urls)} page URLs)"
        )
        return True
    except Exception as e:
        logger.debug(f"MongoDB persistence failed for {report_dict.get('domain_name')}: {e}")
        return False


def save_report_to_mongodb_async(report_dict: Dict[str, Any]) -> None:
    """
    Fire-and-forget background thread to persist the DA/PA report & all page URLs
    into MongoDB without delaying the Web UI or CLI response.
    """
    if not get_configured_mongo_uri():
        return
    t = threading.Thread(
        target=save_report_to_mongodb_sync,
        args=(report_dict,),
        daemon=True,
    )
    t.start()
