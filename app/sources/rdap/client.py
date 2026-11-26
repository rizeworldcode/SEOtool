"""
RFC 7483 / RFC 9083 RDAP Client and Parser.
Queries authoritative registry RDAP endpoints with anti-SSRF protections,
extracts structured registration data, and handles GDPR privacy redactions.
"""
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from urllib.parse import urljoin
from pydantic import BaseModel, Field
import httpx
from loguru import logger
from app.config.settings import settings
from app.security.ssrf_guard import create_safe_httpx_client
from app.sources.rdap.bootstrap import RDAPBootstrapResolver


class RDAPDomainRecord(BaseModel):
    """Normalized domain registration record parsed from RFC 7483 RDAP response."""
    domain_name: str
    registry_domain_id: Optional[str] = None
    registrar_name: Optional[str] = None
    registrar_iana_id: Optional[str] = None
    registration_status: List[str] = Field(default_factory=list)
    created_date: Optional[datetime] = None
    updated_date: Optional[datetime] = None
    expiration_date: Optional[datetime] = None
    nameservers: List[str] = Field(default_factory=list)
    registrant_organization: Optional[str] = None
    registrant_country: Optional[str] = None
    is_privacy_redacted: bool = False
    raw_rdap_json: str
    queried_endpoint: str


class RDAPClient:
    """Client for authoritative RDAP domain lookups."""

    def __init__(
        self,
        resolver: Optional[RDAPBootstrapResolver] = None,
        client: Optional[httpx.Client] = None,
    ):
        self.resolver = resolver or RDAPBootstrapResolver()
        self.client = client or create_safe_httpx_client(
            timeout=15.0,
            headers={
                "User-Agent": settings.CRAWLER_USER_AGENT,
                "Accept": "application/rdap+json, application/json",
            },
        )

    def fetch_domain_rdap(self, domain_name: str) -> Optional[RDAPDomainRecord]:
        """
        Query authoritative RDAP registry endpoint for target domain and parse response.
        Returns RDAPDomainRecord or None if domain not found (HTTP 404).
        """
        clean_domain = domain_name.strip().lower()
        base_endpoint = self.resolver.resolve_domain_endpoint(clean_domain)
        if not base_endpoint:
            logger.warning(f"No authoritative RDAP endpoint discovered for domain: {clean_domain}")
            return None

        # Build full query URL e.g. https://rdap.verisign.com/com/v1/domain/{domain}
        query_url = urljoin(base_endpoint, f"domain/{clean_domain}")
        logger.info(f"Querying authoritative RDAP endpoint for {clean_domain} at {query_url}")

        try:
            response = self.client.get(query_url)
            if response.status_code == 404:
                logger.warning(f"Domain not found in authoritative RDAP registry (HTTP 404): {clean_domain}")
                return None
            response.raise_for_status()
            data = response.json()
            return self._parse_rdap_response(clean_domain, query_url, data)
        except Exception as e:
            logger.error(f"RDAP query failed for {clean_domain} ({e})")
            raise

    def _parse_rdap_response(self, domain_name: str, query_url: str, data: Dict[str, Any]) -> RDAPDomainRecord:
        """Parse RFC 7483 JSON into structured RDAPDomainRecord."""
        registry_domain_id = data.get("handle")
        statuses = data.get("status", [])

        # Parse Events (dates)
        created_date = None
        updated_date = None
        expiration_date = None

        for event in data.get("events", []):
            action = event.get("eventAction", "").lower()
            date_str = event.get("eventDate")
            if not date_str:
                continue
            try:
                # Normalize ISO 8601 strings (replace trailing Z with +00:00)
                clean_date = date_str.replace("Z", "+00:00")
                parsed_dt = datetime.fromisoformat(clean_date)
                if "registration" in action:
                    created_date = parsed_dt
                elif "expiration" in action:
                    expiration_date = parsed_dt
                elif "last changed" in action or "update" in action:
                    updated_date = parsed_dt
            except Exception as e:
                logger.debug(f"Could not parse RDAP event date '{date_str}': {e}")

        # Parse Nameservers
        nameservers = []
        for ns in data.get("nameservers", []):
            name = ns.get("ldhName") or ns.get("unicodeName")
            if name:
                nameservers.append(name.strip().lower())

        # Parse Entities (Registrar, Registrant)
        registrar_name = None
        registrar_iana_id = None
        registrant_org = None
        registrant_country = None
        is_redacted = False

        # Check notices and remarks for redaction keywords
        for notice in data.get("notices", []) + data.get("remarks", []):
            title = (notice.get("title") or "").lower()
            desc = " ".join(notice.get("description", [])).lower()
            if any(k in title or k in desc for k in ("redact", "gdpr", "privacy", "masked", "withheld")):
                is_redacted = True

        for entity in data.get("entities", []):
            roles = [r.lower() for r in entity.get("roles", [])]
            vcard = entity.get("vcardArray", [])

            # Check if entity remarks mention redaction
            for remark in entity.get("remarks", []):
                desc = " ".join(remark.get("description", [])).lower()
                if "redact" in desc or "withheld" in desc:
                    is_redacted = True

            # Registrar
            if "registrar" in roles:
                # Try vcard first
                fn = self._extract_vcard_fn(vcard)
                if fn:
                    registrar_name = fn
                elif entity.get("handle"):
                    registrar_name = entity.get("handle")

                # Public ID (IANA ID)
                for pid in entity.get("publicIds", []):
                    if pid.get("type", "").lower() == "iana registrar id":
                        registrar_iana_id = str(pid.get("identifier"))

            # Registrant
            if "registrant" in roles:
                fn = self._extract_vcard_fn(vcard)
                org = self._extract_vcard_org(vcard)
                country = self._extract_vcard_country(vcard)

                if fn and any(k in fn.lower() for k in ("redact", "privacy", "proxy", "withheld", "gdpr")):
                    is_redacted = True
                if org:
                    registrant_org = org
                elif fn and not is_redacted:
                    registrant_org = fn

                if country:
                    registrant_country = country

        raw_json_str = json.dumps(data)

        return RDAPDomainRecord(
            domain_name=domain_name,
            registry_domain_id=registry_domain_id,
            registrar_name=registrar_name,
            registrar_iana_id=registrar_iana_id,
            registration_status=statuses,
            created_date=created_date,
            updated_date=updated_date,
            expiration_date=expiration_date,
            nameservers=nameservers,
            registrant_organization=registrant_org,
            registrant_country=registrant_country,
            is_privacy_redacted=is_redacted,
            raw_rdap_json=raw_json_str,
            queried_endpoint=query_url,
        )

    def _extract_vcard_fn(self, vcard_array: List) -> Optional[str]:
        """Extract formatted name (fn) from jCard vcardArray."""
        if not vcard_array or len(vcard_array) < 2:
            return None
        properties = vcard_array[1]
        for prop in properties:
            if len(prop) >= 4 and prop[0] == "fn":
                return prop[3]
        return None

    def _extract_vcard_org(self, vcard_array: List) -> Optional[str]:
        """Extract organization from jCard vcardArray."""
        if not vcard_array or len(vcard_array) < 2:
            return None
        properties = vcard_array[1]
        for prop in properties:
            if len(prop) >= 4 and prop[0] == "org":
                val = prop[3]
                if isinstance(val, list) and val:
                    return val[0]
                return str(val)
        return None

    def _extract_vcard_country(self, vcard_array: List) -> Optional[str]:
        """Extract country code from jCard adr property."""
        if not vcard_array or len(vcard_array) < 2:
            return None
        properties = vcard_array[1]
        for prop in properties:
            if len(prop) >= 4 and prop[0] == "adr":
                adr_val = prop[3]
                if isinstance(adr_val, list) and len(adr_val) >= 7:
                    return str(adr_val[6])  # Country is index 6 in RFC 7095 adr
        return None
