"""
Common Crawl Collection Discovery Module.
Discovers available crawl releases dynamically from https://index.commoncrawl.org/collinfo.json.
"""
from typing import List, Dict, Any, Optional
import time
import httpx
from app.config.settings import settings
from app.database.connection import get_db
from app.database.repositories import CrawlRepository
from app.database.models import CrawlCollection
from app.monitoring.logger import logger
from app.security.ssrf_guard import create_safe_httpx_client

COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"


class CommonCrawlDiscovery:
    """Discovers and catalogs available Common Crawl releases."""

    def __init__(self, client: Optional[httpx.Client] = None):
        self.client = client or create_safe_httpx_client(
            headers={"User-Agent": settings.CRAWLER_USER_AGENT}
        )

    def fetch_available_collections(self) -> List[Dict[str, Any]]:
        """Fetch list of active crawl indexes from collinfo.json with retry and db fallback."""
        logger.info(f"Discovering Common Crawl collections from {COLLINFO_URL}")
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.client.get(COLLINFO_URL, timeout=20.0)
                response.raise_for_status()
                data = response.json()
                logger.info(f"Discovered {len(data)} Common Crawl collections.")
                return data
            except (httpx.TransportError, httpx.HTTPError) as e:
                if attempt < max_retries - 1:
                    sleep_time = (2 ** attempt) + 1.0
                    logger.warning(f"Error fetching collinfo.json ({e}), retrying in {sleep_time:.1f}s...")
                    time.sleep(sleep_time)
                else:
                    logger.error(f"Failed to fetch collinfo.json after {max_retries} attempts: {e}")

        # Fallback to collections cached in database
        with get_db() as session:
            cached = session.query(CrawlCollection).all()
            if cached:
                logger.info(f"Using {len(cached)} cached Common Crawl collections from database.")
                return [
                    {
                        "id": c.id,
                        "name": c.name,
                        "cdx-api": c.cdx_api_url,
                        "timegate": c.timegate_url,
                    }
                    for c in cached
                ]

        raise RuntimeError("Could not fetch collinfo.json and no cached collections found in database.")

    def sync_collections_to_db(self) -> List[Dict[str, Any]]:
        """Fetch and persist all available collections in the database."""
        raw_collections = self.fetch_available_collections()
        saved_items = []

        with get_db() as session:
            repo = CrawlRepository(session)
            for item in raw_collections:
                coll_id = item.get("id")
                name = item.get("name", coll_id)
                cdx_url = item.get("cdx-api")
                timegate = item.get("timegate")

                if not coll_id or not cdx_url:
                    continue

                repo.upsert_collection(
                    collection_id=coll_id,
                    name=name,
                    cdx_api_url=cdx_url,
                    timegate_url=timegate,
                )
                saved_items.append({
                    "id": coll_id,
                    "name": name,
                    "cdx_api_url": cdx_url,
                })

        logger.info(f"Synced {len(saved_items)} collections to database.")
        return saved_items

    def get_latest_collection_id(self) -> str:
        """
        Return the crawl ID of the most recent responsive crawl.
        Probes the top collections to ensure the index server is healthy and not 502/504.
        """
        collections = self.fetch_available_collections()
        if not collections:
            raise RuntimeError("No Common Crawl collections available from index.")

        # Check top 5 collections for responsiveness
        for coll in collections[:5]:
            cid = coll.get("id")
            cdx_url = coll.get("cdx-api", f"https://index.commoncrawl.org/{cid}-index")
            try:
                # Fast probe with a single URL query
                r = self.client.get(cdx_url, params={"url": "commoncrawl.org", "output": "json"}, timeout=8.0)
                if r.status_code == 200:
                    logger.info(f"Selected responsive Common Crawl collection: {cid}")
                    return cid
            except Exception as e:
                logger.warning(f"Crawl {cid} index check failed or timed out: {e}")

        # Fallback to the first collection if all probes timed out
        latest_id = collections[0].get("id")
        logger.info(f"Fallback to first Common Crawl collection: {latest_id}")
        return latest_id
