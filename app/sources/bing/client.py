"""
Bing Webmaster Tools API Client.
Conforms to Microsoft's JSON/HTTP WCF legacy service contract (post-August 2026 audited).
Supports API Key query parameter and OAuth 2.0 Bearer token authentication.
"""
from typing import List, Dict, Optional, Any
from urllib.parse import urljoin
from pydantic import BaseModel
import httpx
from loguru import logger
from app.config.settings import settings
from app.security.ssrf_guard import create_safe_httpx_client

BING_API_BASE_URL = "https://ssl.bing.com/webmaster/api.svc/json/"


class BingSite(BaseModel):
    url: str
    authentication_code: Optional[str] = None


class BingLinkSummary(BaseModel):
    referring_domain_url: str
    count: int


class BingLinkCountsResult(BaseModel):
    links: List[BingLinkSummary]
    total_pages: int


class BingUrlDetail(BaseModel):
    referring_page_url: str
    anchor_text: Optional[str] = None


class BingUrlLinksResult(BaseModel):
    details: List[BingUrlDetail]
    total_pages: int


class BingWebmasterClient:
    """Client for the Bing Webmaster Tools JSON/HTTP API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        access_token: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ):
        self.api_key = api_key
        self.access_token = access_token
        self.client = client or create_safe_httpx_client(
            timeout=20.0,
            headers={"User-Agent": settings.CRAWLER_USER_AGENT},
        )

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _get_params(self, base_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = dict(base_params or {})
        if self.api_key and not self.access_token:
            params["apikey"] = self.api_key
        return params

    def probe_service(self) -> Dict[str, Any]:
        """
        Probe Bing Webmaster API endpoints to verify post-August 2026 service availability.
        Tests whether the JSON endpoint is alive (returns 400 with WCF error) rather than 404/410.
        """
        probe_url = urljoin(BING_API_BASE_URL, "GetUserSites")
        logger.info(f"Probing Bing Webmaster service reachability at {probe_url}")
        try:
            r = self.client.get(probe_url, headers=self._get_headers(), params=self._get_params())
            # HTTP 200 = Authorized success, HTTP 400 with JSON = Active WCF endpoint
            is_active = (r.status_code == 200) or (r.status_code == 400 and "ErrorCode" in r.text)
            return {
                "endpoint": probe_url,
                "http_status": r.status_code,
                "is_active_post_sunset": is_active,
                "response_body": r.text[:200],
                "authenticated": (r.status_code == 200),
            }
        except Exception as e:
            logger.error(f"Bing Webmaster service probe failed: {e}")
            return {
                "endpoint": probe_url,
                "http_status": None,
                "is_active_post_sunset": False,
                "error": str(e),
                "authenticated": False,
            }

    def get_user_sites(self) -> List[BingSite]:
        """Call GetUserSites to retrieve verified sites for the authorized account."""
        url = urljoin(BING_API_BASE_URL, "GetUserSites")
        logger.info(f"Querying Bing Webmaster GetUserSites at {url}")
        response = self.client.get(url, headers=self._get_headers(), params=self._get_params())
        response.raise_for_status()
        data = response.json()
        
        # Schema: {"d": [{"__type": "Site:...", "Url": str, "AuthenticationCode": str}]}
        sites: List[BingSite] = []
        raw_list = data.get("d", []) if isinstance(data, dict) else []
        for item in raw_list:
            site_url = item.get("Url")
            if site_url:
                sites.append(BingSite(
                    url=site_url,
                    authentication_code=item.get("AuthenticationCode"),
                ))
        return sites

    def get_link_counts(self, site_url: str, page: int = 0) -> BingLinkCountsResult:
        """Call GetLinkCounts to retrieve referring domain counts for a site."""
        url = urljoin(BING_API_BASE_URL, "GetLinkCounts")
        params = self._get_params({"siteUrl": site_url, "page": str(page)})
        logger.info(f"Querying Bing Webmaster GetLinkCounts for {site_url} (page {page})")
        response = self.client.get(url, headers=self._get_headers(), params=params)
        response.raise_for_status()
        data = response.json()

        # Schema: {"d": {"__type": "LinkCounts:...", "Links": [{"Url": str, "Count": int}], "TotalPages": int}}
        d = data.get("d", {}) if isinstance(data, dict) else {}
        total_pages = d.get("TotalPages", 0)
        links: List[BingLinkSummary] = []
        for item in d.get("Links", []):
            u = item.get("Url")
            cnt = item.get("Count", 0)
            if u:
                links.append(BingLinkSummary(referring_domain_url=u, count=cnt))

        return BingLinkCountsResult(links=links, total_pages=total_pages)

    def get_url_links(self, site_url: str, link: str, page: int = 0) -> BingUrlLinksResult:
        """Call GetUrlLinks to retrieve specific referring URLs and anchor texts."""
        url = urljoin(BING_API_BASE_URL, "GetUrlLinks")
        params = self._get_params({"siteUrl": site_url, "link": link, "page": str(page)})
        logger.info(f"Querying Bing Webmaster GetUrlLinks for site {site_url}, ref {link} (page {page})")
        response = self.client.get(url, headers=self._get_headers(), params=params)
        response.raise_for_status()
        data = response.json()

        # Schema: {"d": {"__type": "UrlLinks:...", "Details": [{"Url": str, "AnchorText": str}], "TotalPages": int}}
        d = data.get("d", {}) if isinstance(data, dict) else {}
        total_pages = d.get("TotalPages", 0)
        details: List[BingUrlDetail] = []
        for item in d.get("Details", []):
            u = item.get("Url")
            anchor = item.get("AnchorText")
            if u:
                details.append(BingUrlDetail(referring_page_url=u, anchor_text=anchor))

        return BingUrlLinksResult(details=details, total_pages=total_pages)
