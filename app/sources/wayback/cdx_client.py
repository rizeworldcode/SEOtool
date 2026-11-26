"""
Wayback Machine CDX API Client.
Queries https://web.archive.org/cdx/search/cdx with rate-pacing and retry backoff.
"""
import time
from typing import List, Optional
from pydantic import BaseModel
import httpx
from loguru import logger
from app.config.settings import settings
from app.security.ssrf_guard import create_safe_httpx_client

WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"


class WaybackCDXRecord(BaseModel):
    timestamp: str  # e.g. "20201101073521"
    original_url: str
    mimetype: str
    status_code: int
    digest: str  # Base32 SHA-1
    length: int


class WaybackCDXClient:
    """Client for querying the Internet Archive Wayback Machine CDX server."""

    def __init__(self, client: Optional[httpx.Client] = None):
        self.client = client or create_safe_httpx_client(
            timeout=25.0,
            headers={"User-Agent": settings.CRAWLER_USER_AGENT},
        )
        self.last_request_time = 0.0

    def _enforce_rate_limit(self):
        """Enforce request pacing to avoid 429 Too Many Requests from Internet Archive."""
        min_interval = 1.0 / max(0.1, settings.WAYBACK_REQUESTS_PER_SECOND)
        elapsed = time.time() - self.last_request_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self.last_request_time = time.time()

    def query_snapshots(
        self,
        domain_or_url: str,
        limit: int = 10,
        filter_status_200: bool = True,
        collapse_digest: bool = True,
    ) -> List[WaybackCDXRecord]:
        """
        Query Wayback CDX for historical snapshots.
        collapse_digest=True filters out adjacent duplicate snapshots with identical SHA-1 digests.
        """
        self._enforce_rate_limit()
        clean_target = domain_or_url.strip()

        params = {
            "url": clean_target,
            "output": "json",
            "fl": "timestamp,original,mimetype,statuscode,digest,length",
            "limit": str(limit),
        }
        if filter_status_200:
            params["filter"] = "statuscode:200"
        if collapse_digest:
            params["collapse"] = "digest"

        logger.info(f"Querying Wayback CDX for '{clean_target}' (limit: {limit})")

        max_retries = 4
        response = None
        for attempt in range(max_retries):
            try:
                response = self.client.get(WAYBACK_CDX_URL, params=params)
                if response.status_code == 200:
                    break
                if response.status_code in (429, 502, 503, 504) and attempt < max_retries - 1:
                    sleep_time = (2 ** attempt) + 1.5
                    logger.warning(f"Wayback CDX returned HTTP {response.status_code}, retrying in {sleep_time:.1f}s...")
                    time.sleep(sleep_time)
                else:
                    response.raise_for_status()
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < max_retries - 1:
                    sleep_time = (2 ** attempt) + 1.5
                    logger.warning(f"Connection error to Wayback CDX ({e}), retrying in {sleep_time:.1f}s...")
                    time.sleep(sleep_time)
                else:
                    raise

        if response is None:
            raise RuntimeError("Wayback CDX query failed after retries.")

        records: List[WaybackCDXRecord] = []
        try:
            data = response.json()
            if not data or len(data) <= 1:
                logger.info(f"No snapshots found for {clean_target} in Wayback CDX.")
                return []
            
            # First row is header: ['timestamp', 'original', 'mimetype', 'statuscode', 'digest', 'length']
            header = data[0]
            ts_idx = header.index("timestamp")
            orig_idx = header.index("original")
            mime_idx = header.index("mimetype")
            status_idx = header.index("statuscode")
            digest_idx = header.index("digest")
            len_idx = header.index("length")

            for row in data[1:]:
                try:
                    records.append(
                        WaybackCDXRecord(
                            timestamp=str(row[ts_idx]),
                            original_url=str(row[orig_idx]),
                            mimetype=str(row[mime_idx]),
                            status_code=int(row[status_idx]),
                            digest=str(row[digest_idx]),
                            length=int(row[len_idx]) if row[len_idx] else 0,
                        )
                    )
                except (ValueError, IndexError) as err:
                    logger.debug(f"Skipping malformed CDX row {row}: {err}")
        except Exception as e:
            logger.error(f"Failed to parse Wayback CDX response: {e}")
            raise

        logger.info(f"Retrieved {len(records)} Wayback snapshots for {clean_target}")
        return records
