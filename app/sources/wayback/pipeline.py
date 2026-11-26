"""
Wayback Machine Ingestion Pipeline.
Orchestrates CDX snapshot discovery, raw id_ snapshot downloads,
HTML parsing, link extraction, provenance tracking, and database persistence.
"""
from datetime import datetime, timezone
from typing import List, Optional
from pydantic import BaseModel
from loguru import logger
from app.database.connection import get_db
from app.database.repositories import CrawlRepository
from app.database.models import CrawlRun, HistoricalSnapshot
from app.sources.wayback.cdx_client import WaybackCDXClient, WaybackCDXRecord
from app.sources.wayback.snapshot_downloader import WaybackSnapshotDownloader, RawSnapshotResult
from app.processors.html_processor import HTMLProcessor
from app.processors.link_extractor import LinkExtractor


class WaybackPipelineResult(BaseModel):
    target: str
    status: str
    crawl_run_id: Optional[int] = None
    snapshots_discovered: int = 0
    snapshots_ingested: int = 0
    total_bytes_downloaded: int = 0
    errors_count: int = 0


class WaybackPipeline:
    """Ingests historical snapshots and link occurrences from the Wayback Machine."""

    def __init__(
        self,
        cdx_client: Optional[WaybackCDXClient] = None,
        downloader: Optional[WaybackSnapshotDownloader] = None,
    ):
        self.cdx_client = cdx_client or WaybackCDXClient()
        self.downloader = downloader or WaybackSnapshotDownloader()

    def run(self, domain_or_url: str, limit: int = 5) -> WaybackPipelineResult:
        clean_target = domain_or_url.strip()
        logger.info(f"Starting Wayback Machine pipeline for '{clean_target}' (limit: {limit})")

        # 1. Initialize crawl run in database
        with get_db() as session:
            repo = CrawlRepository(session)
            crawl_run = repo.create_crawl_run(
                source_id="wayback",
                run_type="historical_snapshots",
                target_domain=clean_target,
            )
            crawl_run_id = crawl_run.id

        # 2. Query CDX for historical snapshots
        try:
            records: List[WaybackCDXRecord] = self.cdx_client.query_snapshots(
                clean_target, limit=limit, filter_status_200=True, collapse_digest=True
            )
        except Exception as e:
            with get_db() as session:
                repo = CrawlRepository(session)
                repo.record_error(
                    source_id="wayback",
                    crawl_run_id=crawl_run_id,
                    stage="cdx_query",
                    error_code="WAYBACK_CDX_ERROR",
                    error_message=str(e),
                )
                run = session.get(CrawlRun, crawl_run_id)
                if run:
                    run.status = "failed"
                    run.completed_at = datetime.now(timezone.utc)
                    run.errors_count = 1
            raise

        total_discovered = len(records)
        total_ingested = 0
        total_bytes = 0
        total_errors = 0

        # Update crawl run discovered count
        with get_db() as session:
            run = session.get(CrawlRun, crawl_run_id)
            if run:
                run.records_discovered = total_discovered

        # 3. Download and process each snapshot
        for idx, cdx_rec in enumerate(records):
            logger.info(f"[{idx + 1}/{total_discovered}] Processing snapshot: {cdx_rec.timestamp} for {cdx_rec.original_url}")
            try:
                # Fetch raw unmodified snapshot
                snap_res: RawSnapshotResult = self.downloader.fetch_raw_snapshot(
                    timestamp=cdx_rec.timestamp,
                    original_url=cdx_rec.original_url,
                )
                total_bytes += snap_res.downloaded_bytes

                # Parse HTML metadata and links
                meta = HTMLProcessor.parse_metadata(snap_res.html_payload)
                links = LinkExtractor.extract_links(snap_res.html_payload, cdx_rec.original_url)

                # Parse captured timestamp (YYYYMMDDhhmmss)
                captured_at = None
                try:
                    captured_at = datetime.strptime(cdx_rec.timestamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                except Exception:
                    pass

                # Database persistence
                with get_db() as session:
                    repo = CrawlRepository(session)

                    # Create provenance
                    prov = repo.record_provenance(
                        source_id="wayback",
                        dataset_identifier="wayback_archive",
                        record_locator=snap_res.snapshot_url,
                        warc_record_id=None,
                        warc_type="response",
                        http_status=snap_res.http_status,
                        content_type=snap_res.content_type,
                        downloaded_bytes=snap_res.downloaded_bytes,
                        payload_sha256=snap_res.payload_sha256,
                        warc_payload_digest=cdx_rec.digest,
                    )

                    # Get or create canonical page
                    page = repo.get_or_create_page(
                        raw_url=cdx_rec.original_url,
                        normalized_url=cdx_rec.original_url.rstrip("/").lower(),
                        http_status=snap_res.http_status,
                        mime_type=snap_res.content_type,
                        provenance_id=prov.id,
                    )

                    # Upsert historical snapshot
                    repo.upsert_historical_snapshot(
                        domain_name=clean_target,
                        page_id=page.id,
                        timestamp=cdx_rec.timestamp,
                        snapshot_url=snap_res.snapshot_url,
                        original_url=cdx_rec.original_url,
                        http_status=snap_res.http_status,
                        mime_type=snap_res.content_type,
                        digest=cdx_rec.digest,
                        content_sha256=snap_res.payload_sha256,
                        downloaded_bytes=snap_res.downloaded_bytes,
                        title=meta.title,
                        meta_description=meta.meta_description,
                        outbound_links_count=len(links),
                        captured_at=captured_at,
                        crawl_run_id=crawl_run_id,
                        provenance_id=prov.id,
                    )

                    # Store link occurrences for the snapshot
                    for link_item in links:
                        target_page = repo.get_or_create_page(
                            raw_url=link_item.raw_target_url,
                            normalized_url=link_item.normalized_target_url,
                        )
                        page_link = repo.get_or_create_page_link(
                            source_page_id=page.id,
                            target_page_id=target_page.id,
                            source_domain_id=page.domain_id,
                            target_domain_id=target_page.domain_id,
                            provenance_id=prov.id,
                        )
                        repo.record_link_occurrence(
                            page_link_id=page_link.id,
                            crawl_run_id=crawl_run_id,
                            source_id="wayback",
                            raw_target_url=link_item.raw_target_url,
                            normalized_target_url=link_item.normalized_target_url,
                            anchor_text=link_item.anchor_text,
                            rel_attributes=link_item.rel_attributes,
                            link_type=link_item.link_type,
                            link_position=link_item.link_position,
                            dom_context=link_item.dom_context,
                            provenance_id=prov.id,
                        )

                total_ingested += 1

            except Exception as e:
                total_errors += 1
                logger.error(f"Error processing snapshot {cdx_rec.timestamp} ({e})")
                with get_db() as session:
                    repo = CrawlRepository(session)
                    repo.record_error(
                        source_id="wayback",
                        crawl_run_id=crawl_run_id,
                        stage="snapshot_processing",
                        error_code="SNAPSHOT_INGEST_ERROR",
                        error_message=str(e),
                        record_locator=f"{cdx_rec.timestamp}:{cdx_rec.original_url}",
                    )

        # 4. Finalize crawl run state
        with get_db() as session:
            run = session.get(CrawlRun, crawl_run_id)
            if run:
                run.status = "completed" if total_ingested > 0 or total_discovered == 0 else "failed"
                run.completed_at = datetime.now(timezone.utc)
                run.bytes_downloaded = total_bytes
                run.records_ingested = total_ingested
                run.errors_count = total_errors

        logger.info(f"Wayback pipeline finished: {total_ingested}/{total_discovered} snapshots ingested, {total_bytes:,} bytes, {total_errors} errors.")

        return WaybackPipelineResult(
            target=clean_target,
            status="completed" if total_errors == 0 else "completed_with_errors",
            crawl_run_id=crawl_run_id,
            snapshots_discovered=total_discovered,
            snapshots_ingested=total_ingested,
            total_bytes_downloaded=total_bytes,
            errors_count=total_errors,
        )
