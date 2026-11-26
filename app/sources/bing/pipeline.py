"""
Bing Webmaster Tools Ingestion Pipeline.
Orchestrates referring domain discovery (GetLinkCounts), referring URL and anchor text
extraction (GetUrlLinks), and database persistence with strict provenance tracking.
"""
import os
import json
import hashlib
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from loguru import logger
from app.database.connection import get_db
from app.database.repositories import CrawlRepository
from app.database.models import CrawlRun
from app.sources.bing.client import (
    BingWebmasterClient,
    BingLinkCountsResult,
    BingUrlLinksResult,
    BingLinkSummary,
    BingUrlDetail,
)


class BingPipelineResult(BaseModel):
    site_url: str
    status: str
    crawl_run_id: Optional[int] = None
    referring_domains_discovered: int = 0
    referring_urls_ingested: int = 0
    total_bytes_processed: int = 0
    errors_count: int = 0
    is_mock: bool = False


class BingAuthenticationError(Exception):
    """Raised when Bing Webmaster API credentials are missing or unauthorized."""
    pass


class BingPipeline:
    """Manages Bing Webmaster API ingestion, credential lookup, and audit logging."""

    def __init__(self, client: Optional[BingWebmasterClient] = None):
        self.client = client

    def _resolve_client(self) -> BingWebmasterClient:
        """Resolve client using database credentials or environment variables."""
        if self.client:
            return self.client

        api_key = os.getenv("BING_WEBMASTER_API_KEY")
        access_token = os.getenv("BING_ACCESS_TOKEN")

        # Check encrypted credentials in database
        if not api_key and not access_token:
            with get_db() as session:
                repo = CrawlRepository(session)
                api_key = repo.get_credential("bing", "api_key")
                access_token = repo.get_credential("bing", "oauth2_token")

        return BingWebmasterClient(api_key=api_key, access_token=access_token)

    def run(
        self,
        site_url: str,
        max_domains: int = 5,
        max_urls_per_domain: int = 5,
        mock: bool = False,
    ) -> BingPipelineResult:
        """
        Execute Bing Webmaster ingestion pipeline.
        If mock=True, uses verified schema mock payloads for pipeline testing.
        """
        clean_site = site_url.strip()
        logger.info(f"Starting Bing Webmaster pipeline for site: {clean_site} (mock={mock})")

        # 1. Initialize crawl run for audit
        with get_db() as session:
            repo = CrawlRepository(session)
            crawl_run = repo.create_crawl_run(
                source_id="bing",
                run_type="bing_backlink_sync",
                target_domain=clean_site,
            )
            crawl_run_id = crawl_run.id

        client = self._resolve_client()

        # 2. Check credentials unless mock mode is active
        if not mock and not client.api_key and not client.access_token:
            probe = client.probe_service()
            with get_db() as session:
                repo = CrawlRepository(session)
                repo.record_sync_history(
                    source_id="bing",
                    operation_name="GetUserSites",
                    status="unverified_missing_credentials",
                    crawl_run_id=crawl_run_id,
                    http_status=probe.get("http_status"),
                    error_message="Missing Bing API key or Bearer token. Use './dataengine bing set-credentials'.",
                )
            raise BingAuthenticationError(
                "No Bing Webmaster API key or OAuth token configured. "
                "Use './dataengine bing set-credentials --api-key <KEY>' or pass mock=True for schema verification."
            )

        # 3. Discover referring domains (GetLinkCounts)
        ref_domains: List[BingLinkSummary] = []
        if mock:
            logger.info("Using mock Bing GetLinkCounts test data for schema verification.")
            ref_domains = [
                BingLinkSummary(referring_domain_url="https://referring-site-alpha.com", count=14),
                BingLinkSummary(referring_domain_url="https://industry-news-beta.org", count=8),
            ]
        else:
            try:
                link_counts_res: BingLinkCountsResult = client.get_link_counts(site_url=clean_site, page=0)
                ref_domains = link_counts_res.links[:max_domains]
                with get_db() as session:
                    repo = CrawlRepository(session)
                    repo.record_sync_history(
                        source_id="bing",
                        operation_name="GetLinkCounts",
                        status="success",
                        crawl_run_id=crawl_run_id,
                        items_count=len(ref_domains),
                        http_status=200,
                    )
            except Exception as e:
                with get_db() as session:
                    repo = CrawlRepository(session)
                    repo.record_error(
                        source_id="bing",
                        crawl_run_id=crawl_run_id,
                        stage="get_link_counts",
                        error_code="BING_LINK_COUNTS_ERROR",
                        error_message=str(e),
                    )
                raise

        # 4. Ingest referring URLs (GetUrlLinks)
        total_urls = 0
        total_bytes = 0
        total_errors = 0

        # Ensure target page exists in DB
        with get_db() as session:
            repo = CrawlRepository(session)
            target_page = repo.get_or_create_page(
                raw_url=clean_site,
                normalized_url=clean_site.rstrip("/").lower(),
            )
            target_page_id = target_page.id
            target_domain_id = target_page.domain_id

        for dom_item in ref_domains:
            details: List[BingUrlDetail] = []
            if mock:
                details = [
                    BingUrlDetail(
                        referring_page_url=f"{dom_item.referring_domain_url}/article-1",
                        anchor_text="Zoniraz Official Store",
                    ),
                    BingUrlDetail(
                        referring_page_url=f"{dom_item.referring_domain_url}/partner-review",
                        anchor_text="Online Jewellery",
                    ),
                ][:max_urls_per_domain]
            else:
                try:
                    res: BingUrlLinksResult = client.get_url_links(
                        site_url=clean_site,
                        link=dom_item.referring_domain_url,
                        page=0,
                    )
                    details = res.details[:max_urls_per_domain]
                    with get_db() as session:
                        repo = CrawlRepository(session)
                        repo.record_sync_history(
                            source_id="bing",
                            operation_name="GetUrlLinks",
                            status="success",
                            crawl_run_id=crawl_run_id,
                            items_count=len(details),
                            http_status=200,
                        )
                except Exception as e:
                    total_errors += 1
                    logger.error(f"Error fetching Bing URL links for {dom_item.referring_domain_url}: {e}")
                    continue

            # Persist referring links and occurrences
            raw_payload = json.dumps([d.model_dump() for d in details])
            payload_sha256 = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
            total_bytes += len(raw_payload.encode("utf-8"))

            with get_db() as session:
                repo = CrawlRepository(session)
                
                # Provenance for Bing API response
                prov = repo.record_provenance(
                    source_id="bing",
                    dataset_identifier="bing_webmaster_api_v1",
                    record_locator=f"{clean_site}:{dom_item.referring_domain_url}",
                    http_status=200,
                    content_type="application/json",
                    downloaded_bytes=len(raw_payload.encode("utf-8")),
                    payload_sha256=payload_sha256,
                )

                for det in details:
                    # Create source referring page
                    source_page = repo.get_or_create_page(
                        raw_url=det.referring_page_url,
                        normalized_url=det.referring_page_url.rstrip("/").lower(),
                        provenance_id=prov.id,
                    )

                    # Create canonical directed page-to-page link
                    page_link = repo.get_or_create_page_link(
                        source_page_id=source_page.id,
                        target_page_id=target_page_id,
                        source_domain_id=source_page.domain_id,
                        target_domain_id=target_domain_id,
                        provenance_id=prov.id,
                    )

                    # Record physical link occurrence with anchor text
                    repo.record_link_occurrence(
                        page_link_id=page_link.id,
                        crawl_run_id=crawl_run_id,
                        source_id="bing",
                        raw_target_url=clean_site,
                        normalized_target_url=clean_site.rstrip("/").lower(),
                        anchor_text=det.anchor_text,
                        link_type="a_href",
                        dom_context="content",
                        provenance_id=prov.id,
                    )
                    total_urls += 1

        # 5. Finalize crawl run
        with get_db() as session:
            run = session.get(CrawlRun, crawl_run_id)
            if run:
                run.status = "completed"
                run.completed_at = datetime.now(timezone.utc)
                run.records_discovered = len(ref_domains)
                run.records_ingested = total_urls
                run.bytes_downloaded = total_bytes
                run.errors_count = total_errors

        logger.info(f"Bing pipeline finished: {total_urls} URLs ingested from {len(ref_domains)} referring domains.")

        return BingPipelineResult(
            site_url=clean_site,
            status="completed",
            crawl_run_id=crawl_run_id,
            referring_domains_discovered=len(ref_domains),
            referring_urls_ingested=total_urls,
            total_bytes_processed=total_bytes,
            errors_count=total_errors,
            is_mock=mock,
        )
