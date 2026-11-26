"""
RDAP Ingestion Pipeline.
Handles authoritative RDAP lookup, 7-day TTL caching, provenance recording,
and database persistence for domain registration records.
"""
import json
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional
from pydantic import BaseModel
from loguru import logger
from app.database.connection import get_db
from app.database.repositories import CrawlRepository
from app.database.models import CrawlRun, DomainRegistrationData
from app.sources.rdap.client import RDAPClient, RDAPDomainRecord


class RDAPPipelineResult(BaseModel):
    domain_name: str
    status: str  # "completed", "cached", "not_found", "failed"
    is_cached: bool = False
    crawl_run_id: Optional[int] = None
    registrar_name: Optional[str] = None
    created_date: Optional[datetime] = None
    expiration_date: Optional[datetime] = None
    nameservers_count: int = 0
    is_privacy_redacted: bool = False


class RDAPPipeline:
    """Orchestrates RDAP domain registration ingestion with TTL caching."""

    def __init__(self, client: Optional[RDAPClient] = None, cache_days: int = 7):
        self.client = client or RDAPClient()
        self.cache_days = cache_days

    def run(self, domain_name: str, force: bool = False) -> RDAPPipelineResult:
        """
        Run RDAP acquisition for domain_name.
        If cached within self.cache_days and not force, returns cached data.
        """
        clean_domain = domain_name.strip().lower()
        logger.info(f"Starting RDAP pipeline for domain: {clean_domain} (force={force})")

        # 1. Check existing cache in database
        with get_db() as session:
            repo = CrawlRepository(session)
            cached_reg = repo.get_domain_registration(clean_domain)
            now = datetime.now(timezone.utc)

            if cached_reg and not force:
                checked_at = cached_reg.last_checked_at
                if checked_at.tzinfo is None:
                    checked_at = checked_at.replace(tzinfo=timezone.utc)
                age = now - checked_at
                if age < timedelta(days=self.cache_days):
                    logger.info(f"Using cached RDAP registration for {clean_domain} (age: {age.days} days)")
                    ns_list = json.loads(cached_reg.nameservers) if cached_reg.nameservers else []
                    return RDAPPipelineResult(
                        domain_name=clean_domain,
                        status="cached",
                        is_cached=True,
                        registrar_name=cached_reg.registrar_name,
                        created_date=cached_reg.created_date,
                        expiration_date=cached_reg.expiration_date,
                        nameservers_count=len(ns_list),
                        is_privacy_redacted=cached_reg.is_privacy_redacted,
                    )

        # 2. Initialize crawl run for audit and provenance
        with get_db() as session:
            repo = CrawlRepository(session)
            crawl_run = repo.create_crawl_run(
                source_id="rdap",
                run_type="domain_registration",
                target_domain=clean_domain,
            )
            crawl_run_id = crawl_run.id

        # 3. Query authoritative RDAP registry
        try:
            record: Optional[RDAPDomainRecord] = self.client.fetch_domain_rdap(clean_domain)
        except Exception as e:
            with get_db() as session:
                repo = CrawlRepository(session)
                repo.record_error(
                    source_id="rdap",
                    crawl_run_id=crawl_run_id,
                    stage="rdap_query",
                    error_code="RDAP_QUERY_ERROR",
                    error_message=str(e),
                )
                run = session.get(CrawlRun, crawl_run_id)
                if run:
                    run.status = "failed"
                    run.completed_at = datetime.now(timezone.utc)
                    run.errors_count = 1
            raise

        if not record:
            # Domain not found / unregistered
            with get_db() as session:
                run = session.get(CrawlRun, crawl_run_id)
                if run:
                    run.status = "completed"
                    run.completed_at = datetime.now(timezone.utc)
                    run.records_discovered = 0
                    run.records_ingested = 0
            return RDAPPipelineResult(
                domain_name=clean_domain,
                status="not_found",
                is_cached=False,
                crawl_run_id=crawl_run_id,
            )

        # 4. Record provenance and persist registration data
        raw_bytes = record.raw_rdap_json.encode("utf-8")
        payload_sha256 = hashlib.sha256(raw_bytes).hexdigest()

        with get_db() as session:
            repo = CrawlRepository(session)
            
            # Create provenance record
            prov = repo.record_provenance(
                source_id="rdap",
                dataset_identifier="iana_rdap_rfc7483",
                record_locator=record.queried_endpoint,
                downloaded_bytes=len(raw_bytes),
                payload_sha256=payload_sha256,
                http_status=200,
                content_type="application/rdap+json",
            )

            # Upsert domain registration
            reg = repo.upsert_domain_registration(
                domain_name=clean_domain,
                registry_domain_id=record.registry_domain_id,
                registrar_name=record.registrar_name,
                registrar_iana_id=record.registrar_iana_id,
                registration_status=json.dumps(record.registration_status),
                created_date=record.created_date,
                updated_date=record.updated_date,
                expiration_date=record.expiration_date,
                nameservers=json.dumps(record.nameservers),
                registrant_organization=record.registrant_organization,
                registrant_country=record.registrant_country,
                is_privacy_redacted=record.is_privacy_redacted,
                raw_rdap_json=record.raw_rdap_json,
                provenance_id=prov.id,
            )

            # Finalize crawl run
            run = session.get(CrawlRun, crawl_run_id)
            if run:
                run.status = "completed"
                run.completed_at = datetime.now(timezone.utc)
                run.records_discovered = 1
                run.records_ingested = 1
                run.bytes_downloaded = len(raw_bytes)

        logger.info(f"Successfully ingested RDAP registration for {clean_domain} (Registrar: {record.registrar_name})")

        return RDAPPipelineResult(
            domain_name=clean_domain,
            status="completed",
            is_cached=False,
            crawl_run_id=crawl_run_id,
            registrar_name=record.registrar_name,
            created_date=record.created_date,
            expiration_date=record.expiration_date,
            nameservers_count=len(record.nameservers),
            is_privacy_redacted=record.is_privacy_redacted,
        )
