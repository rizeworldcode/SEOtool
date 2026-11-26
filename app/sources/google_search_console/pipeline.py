"""
Google Search Console Dual Ingestion Pipeline.
Handles:
1. Links report CSV file imports with strict 'gsc_export' provenance.
2. Search Analytics performance queries with 'gsc_api' provenance and audit logging.
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from loguru import logger
from app.database.connection import get_db
from app.database.repositories import CrawlRepository
from app.database.models import CrawlRun
from app.sources.google_search_console.links_importer import GSCLinksImporter, GSCImportResult
from app.sources.google_search_console.api_client import (
    GSCAPIClient,
    GSCSearchAnalyticsResult,
    GSCSearchAnalyticsRow,
)


class GSCAnalyticsSyncResult(BaseModel):
    site_url: str
    status: str
    crawl_run_id: Optional[int] = None
    rows_synced: int = 0
    is_mock: bool = False


class GSCPipeline:
    """Unified pipeline for Google Search Console data ingestion."""

    def __init__(
        self,
        api_client: Optional[GSCAPIClient] = None,
        importer: Optional[GSCLinksImporter] = None,
    ):
        self.api_client = api_client or GSCAPIClient()
        self.importer = importer or GSCLinksImporter()

    def import_links_file(self, file_path: str, site_url: str) -> GSCImportResult:
        """Import user-exported Search Console Links CSV file."""
        return self.importer.import_links_report(file_path=file_path, target_site=site_url)

    def sync_analytics(
        self,
        site_url: str,
        start_date: str,
        end_date: str,
        mock: bool = False,
    ) -> GSCAnalyticsSyncResult:
        """Sync Search Analytics performance data via GSC API."""
        clean_site = site_url.strip()
        logger.info(f"Starting GSC Search Analytics sync for {clean_site} (mock={mock})")

        with get_db() as session:
            repo = CrawlRepository(session)
            crawl_run = repo.create_crawl_run(
                source_id="gsc_api",
                run_type="gsc_search_analytics_sync",
                target_domain=clean_site,
            )
            crawl_run_id = crawl_run.id

        if mock:
            # Verified schema mock performance data
            mock_rows = [
                GSCSearchAnalyticsRow(
                    keys=["online jewellery store", clean_site],
                    clicks=124.0,
                    impressions=2450.0,
                    ctr=0.0506,
                    position=4.2,
                ),
                GSCSearchAnalyticsRow(
                    keys=["gold diamond rings", f"{clean_site}/rings"],
                    clicks=88.0,
                    impressions=1820.0,
                    ctr=0.0483,
                    position=5.8,
                ),
            ]
            with get_db() as session:
                repo = CrawlRepository(session)
                repo.record_sync_history(
                    source_id="gsc_api",
                    operation_name="searchAnalytics.query",
                    status="success_mock",
                    crawl_run_id=crawl_run_id,
                    items_count=len(mock_rows),
                    http_status=200,
                )
                run = session.get(CrawlRun, crawl_run_id)
                if run:
                    run.status = "completed"
                    run.records_discovered = len(mock_rows)
                    run.records_ingested = len(mock_rows)

            return GSCAnalyticsSyncResult(
                site_url=clean_site,
                status="completed",
                crawl_run_id=crawl_run_id,
                rows_synced=len(mock_rows),
                is_mock=True,
            )
        else:
            if not self.api_client.access_token:
                raise RuntimeError("No Google Search Console OAuth2 access token provided. Use mock=True or set OAuth token.")
            res = self.api_client.query_search_analytics(
                site_url=clean_site,
                start_date=start_date,
                end_date=end_date,
            )
            with get_db() as session:
                repo = CrawlRepository(session)
                repo.record_sync_history(
                    source_id="gsc_api",
                    operation_name="searchAnalytics.query",
                    status="success",
                    crawl_run_id=crawl_run_id,
                    items_count=len(res.rows),
                    http_status=200,
                )
                run = session.get(CrawlRun, crawl_run_id)
                if run:
                    run.status = "completed"
                    run.records_discovered = len(res.rows)
                    run.records_ingested = len(res.rows)

            return GSCAnalyticsSyncResult(
                site_url=clean_site,
                status="completed",
                crawl_run_id=crawl_run_id,
                rows_synced=len(res.rows),
                is_mock=False,
            )
