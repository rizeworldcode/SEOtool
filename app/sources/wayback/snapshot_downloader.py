"""
Raw Wayback Snapshot Downloader.
Fetches verbatim original captures using the 'id_' URI modifier to prevent
Wayback Machine toolbar or script injections.
"""
import time
import hashlib
from typing import Optional
from pydantic import BaseModel
import httpx
from loguru import logger
from app.config.settings import settings
from app.security.ssrf_guard import create_safe_httpx_client


class RawSnapshotResult(BaseModel):
    timestamp: str
    original_url: str
    snapshot_url: str
    http_status: int
    content_type: str
    downloaded_bytes: int
    payload_sha256: str
    html_payload: str
    is_toolbar_injected: bool = False


class WaybackSnapshotDownloader:
    """Retrieves verbatim raw HTML snapshots from the Wayback Machine."""

    def __init__(self, client: Optional[httpx.Client] = None):
        self.client = client or create_safe_httpx_client(
            timeout=30.0,
            follow_redirects=True,
            headers={
                "User-Agent": settings.CRAWLER_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

    def fetch_raw_snapshot(self, timestamp: str, original_url: str) -> RawSnapshotResult:
        """
        Fetch unmodified original HTML snapshot using Wayback id_ modifier.
        URL format: https://web.archive.org/web/{timestamp}id_/{original_url}
        """
        snapshot_url = f"https://web.archive.org/web/{timestamp}id_/{original_url}"
        logger.info(f"Fetching raw snapshot: {snapshot_url}")

        max_retries = 3
        response = None
        for attempt in range(max_retries):
            try:
                response = self.client.get(snapshot_url)
                if response.status_code == 200:
                    break
                if response.status_code in (429, 502, 503, 504) and attempt < max_retries - 1:
                    sleep_time = (2 ** attempt) + 2.0
                    logger.warning(f"Wayback snapshot returned HTTP {response.status_code}, retrying in {sleep_time:.1f}s...")
                    time.sleep(sleep_time)
                else:
                    response.raise_for_status()
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < max_retries - 1:
                    sleep_time = (2 ** attempt) + 2.0
                    logger.warning(f"Error fetching snapshot ({e}), retrying in {sleep_time:.1f}s...")
                    time.sleep(sleep_time)
                else:
                    raise

        if response is None:
            raise RuntimeError(f"Failed to fetch raw snapshot from {snapshot_url}")

        raw_bytes = response.content
        downloaded_bytes = len(raw_bytes)
        payload_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        html_payload = response.text

        # Verify that Wayback toolbar is NOT injected in id_ mode
        toolbar_injected = (
            "__wm.init" in html_payload
            or "wombat.js" in html_payload
            or "wb-toolbar" in html_payload
        )
        if toolbar_injected:
            logger.warning(f"Wayback toolbar detected in snapshot {snapshot_url} despite id_ modifier!")

        return RawSnapshotResult(
            timestamp=timestamp,
            original_url=original_url,
            snapshot_url=snapshot_url,
            http_status=response.status_code,
            content_type=response.headers.get("content-type", "text/html"),
            downloaded_bytes=downloaded_bytes,
            payload_sha256=payload_sha256,
            html_payload=html_payload,
            is_toolbar_injected=toolbar_injected,
        )
