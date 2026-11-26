"""
IANA RDAP Bootstrap Resolver.
Fetches and caches the official IANA DNS RDAP registry mappings from https://data.iana.org/rdap/dns.json.
"""
import os
import json
import time
from typing import Dict, List, Optional
import httpx
from loguru import logger
from app.config.settings import settings
from app.security.ssrf_guard import create_safe_httpx_client

IANA_DNS_BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
CACHE_FILE_PATH = os.path.join(settings.RAW_DATA_DIR, "iana_rdap_dns.json")
CACHE_TTL_SECONDS = 86400 * 7  # 7 days


class RDAPBootstrapResolver:
    """Resolves authoritative RDAP endpoints for top-level domains (TLDs)."""

    def __init__(self, client: Optional[httpx.Client] = None):
        self.client = client or create_safe_httpx_client(
            headers={"User-Agent": settings.CRAWLER_USER_AGENT}
        )
        self._tld_map: Dict[str, str] = {}
        self._loaded = False

    def load_bootstrap(self, force_refresh: bool = False) -> Dict[str, str]:
        """Load bootstrap mappings from cache or live IANA endpoint."""
        if self._loaded and not force_refresh:
            return self._tld_map

        # Check local cache first
        if not force_refresh and os.path.exists(CACHE_FILE_PATH):
            try:
                mtime = os.path.getmtime(CACHE_FILE_PATH)
                if time.time() - mtime < CACHE_TTL_SECONDS:
                    with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._tld_map = self._parse_services(data)
                    self._loaded = True
                    logger.debug(f"Loaded {len(self._tld_map)} TLD endpoints from local IANA RDAP cache.")
                    return self._tld_map
            except Exception as e:
                logger.warning(f"Failed to read local IANA cache ({e}), fetching fresh copy...")

        # Fetch live from IANA
        logger.info(f"Fetching authoritative RDAP bootstrap mappings from {IANA_DNS_BOOTSTRAP_URL}")
        try:
            response = self.client.get(IANA_DNS_BOOTSTRAP_URL, timeout=15.0)
            response.raise_for_status()
            data = response.json()

            # Cache to disk
            os.makedirs(os.path.dirname(CACHE_FILE_PATH), exist_ok=True)
            with open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f)

            self._tld_map = self._parse_services(data)
            self._loaded = True
            logger.info(f"Loaded and cached {len(self._tld_map)} TLD endpoints from IANA RDAP bootstrap.")
            return self._tld_map
        except Exception as e:
            logger.error(f"Error fetching IANA RDAP bootstrap ({e}); falling back to standard TLD defaults.")
            # Fallback for standard top TLDs if IANA is unreachable
            self._tld_map = {
                "com": "https://rdap.verisign.com/com/v1/",
                "net": "https://rdap.verisign.com/net/v1/",
                "org": "https://rdap.publicinterestregistry.org/rdap/",
                "info": "https://rdap.identitydigital.services/rdap/",
                "io": "https://rdap.nic.io/",
                "co": "https://rdap.nic.co/",
            }
            self._loaded = True
            return self._tld_map

    def _parse_services(self, data: Dict) -> Dict[str, str]:
        """Extract TLD -> Base URL map from IANA bootstrap structure."""
        tld_map = {}
        services = data.get("services", [])
        for entry in services:
            if len(entry) >= 2:
                tlds = entry[0]
                endpoints = entry[1]
                if endpoints:
                    # Prefer HTTPS endpoint
                    chosen = next((ep for ep in endpoints if ep.startswith("https://")), endpoints[0])
                    if not chosen.endswith("/"):
                        chosen += "/"
                    for tld in tlds:
                        tld_map[tld.lower()] = chosen
        return tld_map

    def resolve_tld_endpoint(self, tld: str) -> Optional[str]:
        """Resolve the authoritative RDAP endpoint for a given TLD."""
        self.load_bootstrap()
        clean_tld = tld.strip().lower().lstrip(".")
        return self._tld_map.get(clean_tld)

    def resolve_domain_endpoint(self, domain_name: str) -> Optional[str]:
        """Extract TLD from domain and return the authoritative RDAP endpoint."""
        parts = domain_name.strip().lower().split(".")
        if len(parts) >= 2:
            tld = parts[-1]
            return self.resolve_tld_endpoint(tld)
        return None
