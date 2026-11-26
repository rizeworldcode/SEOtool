"""
Google Search Console API Client (v3 / v1).
Handles verified property discovery, Search Analytics (performance queries),
and URL inspection via official Google APIs.
Clarifies that the standard API does NOT expose backlink data.
"""
from typing import List, Dict, Optional, Any
from pydantic import BaseModel
import httpx
from loguru import logger
from app.config.settings import settings
from app.security.ssrf_guard import create_safe_httpx_client

GSC_API_BASE_URL = "https://www.googleapis.com/webmasters/v3"
GSC_INSPECTION_BASE_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"


class GSCSiteProperty(BaseModel):
    site_url: str
    permission_level: str  # siteOwner, siteFullUser, siteRestrictedUser


class GSCSearchAnalyticsRow(BaseModel):
    keys: List[str]  # e.g. ["jewellery online", "https://zoniraz.com/"]
    clicks: float
    impressions: float
    ctr: float
    position: float


class GSCSearchAnalyticsResult(BaseModel):
    site_url: str
    start_date: str
    end_date: str
    dimensions: List[str]
    rows: List[GSCSearchAnalyticsRow]
    response_aggregation_type: Optional[str] = None


class GSCAPIClient:
    """Official Google Search Console API v3 client."""

    def __init__(
        self,
        access_token: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ):
        self.access_token = access_token
        self.client = client or create_safe_httpx_client(
            timeout=25.0,
            headers={"User-Agent": settings.CRAWLER_USER_AGENT},
        )

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def list_verified_sites(self) -> List[GSCSiteProperty]:
        """Query GET https://www.googleapis.com/webmasters/v3/sites for verified properties."""
        url = f"{GSC_API_BASE_URL}/sites"
        logger.info(f"Querying Google Search Console sites list at {url}")
        response = self.client.get(url, headers=self._get_headers())
        response.raise_for_status()
        data = response.json()

        sites: List[GSCSiteProperty] = []
        for entry in data.get("siteEntry", []):
            u = entry.get("siteUrl")
            perm = entry.get("permissionLevel", "unknown")
            if u:
                sites.append(GSCSiteProperty(site_url=u, permission_level=perm))
        return sites

    def query_search_analytics(
        self,
        site_url: str,
        start_date: str,
        end_date: str,
        dimensions: Optional[List[str]] = None,
        row_limit: int = 1000,
    ) -> GSCSearchAnalyticsResult:
        """
        Query Search Analytics performance data.
        NOTE: This endpoint exposes search performance (clicks, impressions, keywords, ranking),
        NOT backlink or referring domain reports.
        """
        import urllib.parse
        encoded_site = urllib.parse.quote(site_url, safe="")
        url = f"{GSC_API_BASE_URL}/sites/{encoded_site}/searchAnalytics/query"
        
        dims = dimensions or ["query", "page"]
        payload = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": dims,
            "rowLimit": row_limit,
        }

        logger.info(f"Querying Search Analytics for {site_url} from {start_date} to {end_date}")
        response = self.client.post(url, headers=self._get_headers(), json=payload)
        response.raise_for_status()
        data = response.json()

        rows: List[GSCSearchAnalyticsRow] = []
        for r in data.get("rows", []):
            rows.append(
                GSCSearchAnalyticsRow(
                    keys=r.get("keys", []),
                    clicks=float(r.get("clicks", 0.0)),
                    impressions=float(r.get("impressions", 0.0)),
                    ctr=float(r.get("ctr", 0.0)),
                    position=float(r.get("position", 0.0)),
                )
            )

        return GSCSearchAnalyticsResult(
            site_url=site_url,
            start_date=start_date,
            end_date=end_date,
            dimensions=dims,
            rows=rows,
            response_aggregation_type=data.get("responseAggregationType"),
        )

    @staticmethod
    def get_bigquery_export_dataset_schema() -> Dict[str, Any]:
        """
        Official architectural documentation on Google Search Console BigQuery bulk export:
        Google Search Console bulk export creates two daily tables in BigQuery:
        1. `searchdata_site_impression`: Impressions aggregated by property.
        2. `searchdata_url_impression`: Impressions and clicks aggregated by landing page URL.
        CRITICAL ARCHITECTURAL FACT: Google Search Console BigQuery bulk export DOES NOT export
        the Links / backlink report. Backlinks must be imported via the user-exported Links report CSV.
        """
        return {
            "export_dataset": "Google Search Console BigQuery Bulk Export",
            "tables": [
                {
                    "table_name": "searchdata_site_impression",
                    "type": "Performance (Impression)",
                    "contains_backlinks": False,
                },
                {
                    "table_name": "searchdata_url_impression",
                    "type": "Performance (URL Impression & Clicks)",
                    "contains_backlinks": False,
                },
            ],
            "links_report_in_bigquery": False,
            "backlink_import_requirement": "Manual / scheduled user CSV export from Search Console Links UI",
        }
