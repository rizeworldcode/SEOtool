"""
Milestone 1A Pipeline Orchestrator: Common Crawl Site Page and Outbound Link Pipeline.
Discovers pages on target domain, retrieves WARC byte-range records, extracts
metadata & outbound links, and commits all entities with full data provenance.
"""
import json
import traceback
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from pydantic import BaseModel

from app.database.connection import get_db
from app.database.repositories import CrawlRepository
from app.sources.commoncrawl.discovery import CommonCrawlDiscovery
from app.sources.commoncrawl.cdx_client import CommonCrawlCDXClient, CDXRecord
from app.sources.commoncrawl.warc_downloader import CommonCrawlWARCDownloader, WARCDownloadError
from app.processors.html_processor import HTMLProcessor
from app.processors.link_extractor import LinkExtractor
from app.monitoring.logger import logger


class PipelineResult(BaseModel):
    crawl_run_id: int
    target_domain: str
    crawl_id: str
    records_discovered: int
    pages_ingested: int
    links_extracted: int
    bytes_downloaded: int
    errors_count: int
    status: str


class CommonCrawlM1APipeline:
    """Executes Milestone 1A site page and outbound link extraction."""

    def __init__(self):
        self.discovery = CommonCrawlDiscovery()
        self.cdx_client = CommonCrawlCDXClient()
        self.downloader = CommonCrawlWARCDownloader()

    def run(
        self,
        target_domain: str,
        crawl_id: Optional[str] = None,
        max_pages: int = 10,
    ) -> PipelineResult:
        """
        Execute full M1A pipeline for a target domain.
        """
        logger.info(f"Starting Milestone 1A pipeline for target domain: {target_domain}")

        # 1. Resolve crawl collection ID
        if not crawl_id:
            crawl_id = self.discovery.get_latest_collection_id()
            self.discovery.sync_collections_to_db()

        # 2. Initialize crawl run in database
        with get_db() as session:
            repo = CrawlRepository(session)
            crawl_run = repo.create_crawl_run(
                source_id="commoncrawl",
                run_type="cc_site_page_outbound_pipeline",
                target_domain=target_domain,
            )
            crawl_run_id = crawl_run.id

        # 3. Query CDX index for target domain pages
        cdx_records: List[CDXRecord] = self.cdx_client.query_domain(
            crawl_id=crawl_id,
            domain_pattern=target_domain,
            limit=max_pages,
            filter_status_200=True,
            filter_html_only=True,
        )

        total_discovered = len(cdx_records)
        total_ingested = 0
        total_links = 0
        total_bytes = 0
        total_errors = 0

        # Update crawl run with discovered records count
        with get_db() as session:
            from app.database.models import CrawlRun
            run = session.get(CrawlRun, crawl_run_id)
            if run:
                run.records_discovered = total_discovered

        # 4. Iterate and extract each WARC record
        for index, record in enumerate(cdx_records):
            locator = f"{record.filename}:{record.offset}:{record.length}"
            logger.info(f"[{index + 1}/{total_discovered}] Processing: {record.url} (offset {record.offset}, len {record.length})")

            try:
                # A. Download and unpack WARC slice
                warc_data = self.downloader.fetch_warc_slice(
                    filename=record.filename,
                    offset=record.offset,
                    length=record.length,
                    expected_url=record.url,
                )
                total_bytes += warc_data.downloaded_compressed_bytes

                # B. Parse HTML metadata and outbound links
                metadata = HTMLProcessor.parse_metadata(warc_data.html_payload)
                link_occurrences = LinkExtractor.extract_links(
                    html_content=warc_data.html_payload,
                    source_url=record.url,
                )

                # C. Commit to Database
                with get_db() as session:
                    repo = CrawlRepository(session)

                    # 1. Record Provenance
                    prov = repo.record_provenance(
                        source_id="commoncrawl",
                        dataset_identifier=crawl_id,
                        record_locator=locator,
                        warc_record_id=warc_data.warc_record_id,
                        warc_type=warc_data.warc_type,
                        http_status=warc_data.http_status,
                        content_type=warc_data.content_type,
                        warc_truncated=warc_data.warc_truncated,
                        downloaded_bytes=warc_data.downloaded_compressed_bytes,
                        record_compressed_sha256=warc_data.record_compressed_sha256,
                        record_decompressed_sha256=warc_data.record_decompressed_sha256,
                        payload_sha256=warc_data.payload_sha256,
                        warc_payload_digest=warc_data.warc_payload_digest or record.digest,
                    )

                    # 2. Upsert Source Page
                    source_page = repo.get_or_create_page(
                        raw_url=record.url,
                        normalized_url=record.url,
                        http_status=warc_data.http_status,
                        mime_type=warc_data.content_type,
                        canonical_url=metadata.canonical_url,
                        truncation_reason=warc_data.warc_truncated,
                        provenance_id=prov.id,
                    )

                    # 3. Upsert Webpage Metadata
                    repo.upsert_webpage_metadata(
                        page_id=source_page.id,
                        title=metadata.title,
                        meta_description=metadata.meta_description,
                        h1_headings=json.dumps(metadata.h1_headings),
                        h2_headings=json.dumps(metadata.h2_headings),
                        canonical_url=metadata.canonical_url,
                        language=metadata.language,
                        provenance_id=prov.id,
                    )

                    # 4. Ingest Outbound Link Occurrences
                    for occ in link_occurrences:
                        # Ensure target page exists
                        target_page = repo.get_or_create_page(
                            raw_url=occ.raw_target_url,
                            normalized_url=occ.normalized_target_url,
                            provenance_id=prov.id,
                        )

                        # Upsert canonical page link
                        page_link = repo.get_or_create_page_link(
                            source_page_id=source_page.id,
                            target_page_id=target_page.id,
                            source_domain_id=source_page.domain_id,
                            target_domain_id=target_page.domain_id,
                            provenance_id=prov.id,
                        )

                        # Insert individual link occurrence
                        repo.record_link_occurrence(
                            page_link_id=page_link.id,
                            crawl_run_id=crawl_run_id,
                            source_id="commoncrawl",
                            raw_target_url=occ.raw_target_url,
                            normalized_target_url=occ.normalized_target_url,
                            anchor_text=occ.anchor_text,
                            rel_attributes=occ.rel_attributes,
                            link_type=occ.link_type,
                            link_position=occ.link_position,
                            dom_context=occ.dom_context,
                            http_status=warc_data.http_status,
                            provenance_id=prov.id,
                        )
                        total_links += 1

                    total_ingested += 1

            except WARCDownloadError as e:
                total_errors += 1
                logger.warning(f"WARC download error for {record.url}: {e}")
                with get_db() as session:
                    repo = CrawlRepository(session)
                    repo.record_error(
                        source_id="commoncrawl",
                        stage="warc_download",
                        error_code=e.error_code,
                        error_message=str(e),
                        crawl_run_id=crawl_run_id,
                        record_locator=locator,
                        actual_bytes=e.actual_bytes,
                    )

            except Exception as e:
                total_errors += 1
                logger.error(f"Unexpected processing error for {record.url}: {e}")
                with get_db() as session:
                    repo = CrawlRepository(session)
                    repo.record_error(
                        source_id="commoncrawl",
                        stage="processing",
                        error_code="UNEXPECTED_PROCESSING_EXCEPTION",
                        error_message=str(e),
                        crawl_run_id=crawl_run_id,
                        stack_trace=traceback.format_exc(),
                        record_locator=locator,
                    )

        # 5. Finalize crawl run state
        with get_db() as session:
            from app.database.models import CrawlRun
            run = session.get(CrawlRun, crawl_run_id)
            if run:
                run.status = "completed" if total_ingested > 0 else "failed"
                run.completed_at = datetime.now(timezone.utc)
                run.bytes_downloaded = total_bytes
                run.records_ingested = total_ingested
                run.errors_count = total_errors

        logger.info(
            f"Pipeline finished: {total_ingested}/{total_discovered} pages ingested, "
            f"{total_links} links extracted, {total_bytes} bytes downloaded, {total_errors} errors."
        )

        return PipelineResult(
            crawl_run_id=crawl_run_id,
            target_domain=target_domain,
            crawl_id=crawl_id,
            records_discovered=total_discovered,
            pages_ingested=total_ingested,
            links_extracted=total_links,
            bytes_downloaded=total_bytes,
            errors_count=total_errors,
            status="completed" if total_ingested > 0 else "failed",
        )
