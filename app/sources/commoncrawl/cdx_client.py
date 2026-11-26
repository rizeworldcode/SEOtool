"""
Common Crawl CDX Index Client.
Queries https://index.commoncrawl.org/{crawl_id}-index to discover crawled URLs
hosted on target domains with offset, length, and filename locators.
"""
import json
import time
from typing import List, Optional, Generator
from pydantic import BaseModel, Field
import httpx
from app.config.settings import settings
from app.monitoring.logger import logger
from app.security.ssrf_guard import create_safe_httpx_client


class CDXRecord(BaseModel):
    """Structured representation of a Common Crawl CDX index record."""
    url: str
    mime: Optional[str] = None
    status: Optional[int] = None
    digest: Optional[str] = None  # sha1 base32 digest
    length: int  # compressed WARC record length
    offset: int  # byte offset in the WARC file
    filename: str  # S3 key under s3://commoncrawl/ e.g. crawl-data/...
    timestamp: Optional[str] = None


class CommonCrawlCDXClient:
    """Queries Common Crawl CDX server with pacing and structured parsing."""

    def __init__(self, client: Optional[httpx.Client] = None):
        self.client = client or create_safe_httpx_client(
            headers={
                "User-Agent": settings.CRAWLER_USER_AGENT,
                "Connection": "close",
            }
        )
        self.last_request_time = 0.0

    def _enforce_rate_limit(self):
        """Pace requests to respect CDX_REQUESTS_PER_SECOND."""
        min_interval = 1.0 / max(0.1, settings.CDX_REQUESTS_PER_SECOND)
        elapsed = time.time() - self.last_request_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self.last_request_time = time.time()

    def query_domain(
        self,
        crawl_id: str,
        domain_pattern: str,
        limit: int = 100,
        filter_status_200: bool = True,
        filter_html_only: bool = True,
    ) -> List[CDXRecord]:
        """
        Query CDX index for URLs matching domain_pattern (e.g. 'zoniraz.com' or 'example.com').
        Passes clean URL and output=json to the CDX server (avoiding upstream pywb 502/504 timeouts)
        and performs status, MIME, and limit slicing on the client side.
        """
        base_url = f"https://index.commoncrawl.org/{crawl_id}-index"
        
        # Clean domain query: strip trailing /* to avoid slow full-prefix table scans
        clean_domain = domain_pattern.rstrip("/*").strip()

        params = {
            "url": clean_domain,
            "output": "json",
        }

        self._enforce_rate_limit()
        logger.info(f"Querying CDX index at {base_url} for domain: {clean_domain} (client limit: {limit})")

        # Retry on transient 502/503/504 gateway errors or timeouts from the CDX cluster
        max_retries = 5
        response = None
        for attempt in range(max_retries):
            try:
                response = self.client.get(base_url, params=params, timeout=45.0)
                if response.status_code == 200 or response.status_code == 404:
                    break
                if response.status_code in (500, 502, 503, 504) and attempt < max_retries - 1:
                    sleep_time = (2 ** attempt) + 1.0
                    logger.warning(f"Transient HTTP {response.status_code} from CDX, retrying in {sleep_time:.1f}s (attempt {attempt + 1}/{max_retries})...")
                    time.sleep(sleep_time)
                else:
                    response.raise_for_status()
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError, httpx.TransportError) as e:
                # Re-create client to drop dead or reset sockets
                try:
                    self.client.close()
                except Exception:
                    pass
                self.client = create_safe_httpx_client(
                    headers={
                        "User-Agent": settings.CRAWLER_USER_AGENT,
                        "Connection": "close",
                    }
                )
                if attempt < max_retries - 1:
                    sleep_time = (2 ** attempt) + 1.5
                    logger.warning(f"Connection error/timeout querying CDX ({type(e).__name__}: {e}), refreshed client, retrying in {sleep_time:.1f}s (attempt {attempt + 1}/{max_retries})...")
                    time.sleep(sleep_time)
                else:
                    raise

        if response is None:
            raise RuntimeError("CDX query returned no response after retries.")

        if response.status_code == 404:
            logger.warning(f"No records found in CDX index for {domain_pattern} in crawl {crawl_id}")
            return []

        response.raise_for_status()

        records = []
        for line in response.text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                status_code = int(data.get("status", 200))
                if filter_status_200 and status_code != 200:
                    continue

                mime = data.get("mime", "")
                if filter_html_only and mime and "html" not in mime.lower():
                    continue

                rec = CDXRecord(
                    url=data["url"],
                    mime=data.get("mime"),
                    status=status_code,
                    digest=data.get("digest"),
                    length=int(data["length"]),
                    offset=int(data["offset"]),
                    filename=data["filename"],
                    timestamp=data.get("timestamp"),
                )
                records.append(rec)
                if len(records) >= limit:
                    break
            except Exception as e:
                logger.warning(f"Failed to parse CDX JSON line: {line[:100]}... Error: {e}")

        logger.info(f"Retrieved {len(records)} matching CDX records for {domain_pattern}")
        return records
