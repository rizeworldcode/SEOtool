"""
Common Crawl WARC Byte-Range Downloader.
Retrieves individual WARC records using HTTP 206 Partial Content byte ranges
from https://data.commoncrawl.org/ (s3://commoncrawl/).
Validates Content-Range, byte counts, decompression ratios, and truncation metadata.
"""
import io
import hashlib
from typing import Optional, Tuple
from pydantic import BaseModel
import httpx
from warcio.archiveiterator import ArchiveIterator
from app.config.settings import settings
from app.monitoring.logger import logger
from app.security.ssrf_guard import create_safe_httpx_client

DATA_ROOT_URL = "https://data.commoncrawl.org"


class ExtractedWARCRecord(BaseModel):
    """Result of successfully downloading and parsing a WARC byte-range record."""
    target_url: str
    warc_record_id: Optional[str] = None
    warc_type: Optional[str] = None
    http_status: int
    content_type: Optional[str] = None
    warc_truncated: Optional[str] = None
    html_payload: str
    downloaded_compressed_bytes: int
    decompressed_payload_bytes: int
    record_compressed_sha256: str
    record_decompressed_sha256: str
    payload_sha256: str
    warc_payload_digest: Optional[str] = None


class WARCDownloadError(Exception):
    """Raised when WARC byte-range retrieval or decompression fails."""
    def __init__(self, message: str, error_code: str, actual_bytes: Optional[int] = None):
        super().__init__(message)
        self.error_code = error_code
        self.actual_bytes = actual_bytes


class CommonCrawlWARCDownloader:
    """Retrieves and parses atomic WARC byte-range records."""

    def __init__(self, client: Optional[httpx.Client] = None):
        self.client = client or create_safe_httpx_client(
            headers={"User-Agent": settings.CRAWLER_USER_AGENT}
        )

    def fetch_warc_slice(
        self,
        filename: str,
        offset: int,
        length: int,
        expected_url: Optional[str] = None,
    ) -> ExtractedWARCRecord:
        """
        Fetch exact byte-range slice for a WARC record.
        Verifies HTTP 206 Partial Content, exact byte counts, decompression limits,
        and extracts the uncompressed HTML payload.
        """
        # Ensure filename does not have leading slash
        clean_filename = filename.lstrip("/")
        full_url = f"{DATA_ROOT_URL}/{clean_filename}"
        
        start_byte = offset
        end_byte = offset + length - 1
        headers = {
            "Range": f"bytes={start_byte}-{end_byte}",
            "Accept-Encoding": "identity",  # Ensure no intermediate transit compression
        }

        # Check compressed size limit before fetching
        if length > settings.MAX_WARC_RECORD_COMPRESSED_BYTES:
            raise WARCDownloadError(
                f"Requested WARC slice ({length} bytes) exceeds MAX_WARC_RECORD_COMPRESSED_BYTES ({settings.MAX_WARC_RECORD_COMPRESSED_BYTES})",
                error_code="EXCEEDED_COMPRESSED_LIMIT",
                actual_bytes=length,
            )

        logger.debug(f"Fetching WARC byte-range: {full_url} ({start_byte}-{end_byte}, {length} bytes)")
        
        # Retry up to 4 times on transient 503 / 429 errors from AWS S3/CloudFront
        max_retries = 4
        response = None
        for attempt in range(max_retries):
            response = self.client.get(full_url, headers=headers, timeout=30.0)
            if response.status_code == 206:
                break
            if response.status_code in (503, 429) and attempt < max_retries - 1:
                sleep_time = (2 ** attempt) + 1.0
                logger.warning(f"Transient HTTP {response.status_code} from S3, retrying in {sleep_time:.1f}s (attempt {attempt + 1}/{max_retries})...")
                import time
                time.sleep(sleep_time)
            else:
                break

        # 1. Verify HTTP 206 Partial Content
        if response.status_code != 206:
            raise WARCDownloadError(
                f"Expected HTTP 206 Partial Content from {full_url}, got HTTP {response.status_code}",
                error_code="INVALID_HTTP_STATUS",
                actual_bytes=len(response.content) if response else 0,
            )

        compressed_data = response.content
        actual_length = len(compressed_data)

        # 2. Verify exact byte count matches index length
        if actual_length != length:
            raise WARCDownloadError(
                f"Byte count mismatch: expected {length} bytes, received {actual_length} bytes",
                error_code="BYTE_COUNT_MISMATCH",
                actual_bytes=actual_length,
            )

        # 3. Verify Content-Range header
        content_range = response.headers.get("Content-Range", "")
        expected_range_prefix = f"bytes {start_byte}-{end_byte}/"
        if not content_range.startswith(expected_range_prefix):
            logger.warning(f"Content-Range mismatch: expected {expected_range_prefix}*, got '{content_range}'")

        # 4. Calculate compressed SHA-256
        compressed_sha256 = hashlib.sha256(compressed_data).hexdigest()

        # 5. Parse WARC record using warcio streaming decompressor
        stream = io.BytesIO(compressed_data)
        try:
            iterator = ArchiveIterator(stream)
            record = next(iterator, None)
            if not record:
                raise WARCDownloadError("No WARC record found in compressed stream", error_code="EMPTY_WARC_RECORD")
        except Exception as e:
            raise WARCDownloadError(f"Decompression / WARC parsing error: {e}", error_code="MALFORMED_WARC")

        # Extract WARC headers
        warc_headers = record.rec_headers
        target_uri = warc_headers.get_header("WARC-Target-URI", expected_url or "")
        record_id = warc_headers.get_header("WARC-Record-ID")
        record_type = warc_headers.get_header("WARC-Type")
        warc_truncated = warc_headers.get_header("WARC-Truncated")
        warc_payload_digest = warc_headers.get_header("WARC-Payload-Digest")

        # 6. Extract HTTP status, headers, and body payload
        http_status = 200
        content_type = None

        if record.http_headers:
            status_line = record.http_headers.statusline or ""
            parts = status_line.split(" ", 2)
            if len(parts) >= 2 and parts[1].isdigit():
                http_status = int(parts[1])
            content_type = record.http_headers.get_header("Content-Type")

        # Read the HTTP response body payload using content_stream
        payload_bytes = record.content_stream().read()
        payload_len = len(payload_bytes)
        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()

        # Check decompressed payload limit
        if payload_len > settings.MAX_WARC_PAYLOAD_DECOMPRESSED_BYTES:
            raise WARCDownloadError(
                f"Decompressed WARC payload ({payload_len} bytes) exceeds limit ({settings.MAX_WARC_PAYLOAD_DECOMPRESSED_BYTES})",
                error_code="EXCEEDED_DECOMPRESSED_LIMIT",
                actual_bytes=payload_len,
            )

        # Check decompression ratio (catch decompression bombs)
        if actual_length > 0:
            ratio = payload_len / actual_length
            if ratio > settings.MAX_DECOMPRESSION_RATIO:
                raise WARCDownloadError(
                    f"Decompression ratio ({ratio:.1f}) exceeds MAX_DECOMPRESSION_RATIO ({settings.MAX_DECOMPRESSION_RATIO})",
                    error_code="DECOMPRESSION_BOMB_DETECTED",
                    actual_bytes=payload_len,
                )

        # Decompressed record bytes SHA-256
        decompressed_sha256 = hashlib.sha256(payload_bytes).hexdigest()

        # Decode HTML with fallback encodings
        try:
            html_payload = payload_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                html_payload = payload_bytes.decode("latin-1", errors="replace")
            except Exception:
                html_payload = str(payload_bytes)

        logger.debug(f"Successfully unpacked WARC record: {target_uri} (HTTP {http_status}, {len(html_payload)} chars HTML)")

        return ExtractedWARCRecord(
            target_url=target_uri,
            warc_record_id=record_id,
            warc_type=record_type,
            http_status=http_status,
            content_type=content_type,
            warc_truncated=warc_truncated,
            html_payload=html_payload,
            downloaded_compressed_bytes=actual_length,
            decompressed_payload_bytes=payload_len,
            record_compressed_sha256=compressed_sha256,
            record_decompressed_sha256=decompressed_sha256,
            payload_sha256=payload_sha256,
            warc_payload_digest=warc_payload_digest,
        )
