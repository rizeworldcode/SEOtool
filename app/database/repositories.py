"""
Database repositories for LinkPilot Data Engine.
Implements idempotent upserts and transaction management for crawl entities.
"""
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse
from sqlalchemy.orm import Session
from sqlalchemy import select, case
from app.database.models import (
    DataSource,
    CrawlCollection,
    CrawlRun,
    DataProvenance,
    Domain,
    Page,
    PageLink,
    LinkOccurrence,
    WebpageMetadata,
    ProcessingError,
    DownloadFile,
    DomainRegistrationData,
    HistoricalSnapshot,
    SourceCredential,
    SourceSyncHistory,
    PageAuthorityScore,
    DomainAuthorityScore,
)
from app.security.crypto import encrypt_secret, decrypt_secret
from app.monitoring.logger import logger


def normalize_hostname(hostname: str) -> str:
    """Normalize hostname to lowercase and punycode."""
    if not hostname:
        return ""
    hostname = hostname.strip().lower()
    try:
        return hostname.encode("idna").decode("ascii")
    except Exception:
        return hostname


def extract_registered_domain(domain_name: str) -> str:
    """Extract registered domain (simple fallback heuristic, e.g. zoniraz.com)."""
    parts = domain_name.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return domain_name


class CrawlRepository:
    """Repository handling database operations for crawl data ingestion."""

    def __init__(self, session: Session):
        self.session = session

    def get_or_create_source(self, source_id: str, name: str) -> DataSource:
        source = self.session.query(DataSource).filter_by(id=source_id).first()
        if not source:
            source = DataSource(id=source_id, name=name, is_enabled=True, health_status="healthy")
            self.session.add(source)
            self.session.flush()
        return source

    def upsert_collection(self, collection_id: str, name: str, cdx_api_url: str, **kwargs) -> CrawlCollection:
        collection = self.session.query(CrawlCollection).filter_by(id=collection_id).first()
        if not collection:
            collection = CrawlCollection(
                id=collection_id,
                name=name,
                cdx_api_url=cdx_api_url,
                **kwargs
            )
            self.session.add(collection)
        else:
            collection.cdx_api_url = cdx_api_url
            for k, v in kwargs.items():
                setattr(collection, k, v)
        self.session.flush()
        return collection

    def create_crawl_run(self, source_id: str, run_type: str, target_domain: Optional[str] = None) -> CrawlRun:
        self.get_or_create_source(source_id, source_id.replace("_", " ").title())
        run = CrawlRun(
            source_id=source_id,
            run_type=run_type,
            target_domain=target_domain,
            status="running",
        )
        self.session.add(run)
        self.session.flush()
        return run

    def record_provenance(
        self,
        source_id: str,
        dataset_identifier: str,
        record_locator: str,
        warc_record_id: Optional[str] = None,
        warc_type: Optional[str] = None,
        http_status: Optional[int] = None,
        content_type: Optional[str] = None,
        warc_truncated: Optional[str] = None,
        downloaded_bytes: Optional[int] = None,
        record_compressed_sha256: Optional[str] = None,
        record_decompressed_sha256: Optional[str] = None,
        payload_sha256: Optional[str] = None,
        warc_payload_digest: Optional[str] = None,
    ) -> DataProvenance:
        prov = DataProvenance(
            source_id=source_id,
            dataset_identifier=dataset_identifier,
            record_locator=record_locator,
            warc_record_id=warc_record_id,
            warc_type=warc_type,
            http_status=http_status,
            content_type=content_type,
            warc_truncated=warc_truncated,
            downloaded_bytes=downloaded_bytes,
            record_compressed_sha256=record_compressed_sha256,
            record_decompressed_sha256=record_decompressed_sha256,
            payload_sha256=payload_sha256,
            warc_payload_digest=warc_payload_digest,
        )
        self.session.add(prov)
        self.session.flush()
        return prov

    def get_or_create_domain(self, domain_name: str) -> Domain:
        norm_domain = normalize_hostname(domain_name)
        domain = self.session.query(Domain).filter_by(domain_name=norm_domain).first()
        if not domain:
            tld = norm_domain.split(".")[-1] if "." in norm_domain else "unknown"
            reg_domain = extract_registered_domain(norm_domain)
            domain = Domain(
                domain_name=norm_domain,
                registered_domain=reg_domain,
                tld=tld,
                is_punycode="xn--" in norm_domain,
            )
            self.session.add(domain)
            self.session.flush()
        return domain

    def get_or_create_page(
        self,
        raw_url: str,
        normalized_url: str,
        http_status: Optional[int] = None,
        mime_type: Optional[str] = None,
        canonical_url: Optional[str] = None,
        truncation_reason: Optional[str] = None,
        provenance_id: Optional[int] = None,
    ) -> Page:
        parsed = urlparse(normalized_url)
        domain_name = parsed.hostname or "unknown"
        domain = self.get_or_create_domain(domain_name)

        page = self.session.query(Page).filter_by(normalized_url=normalized_url).first()
        if not page:
            page = Page(
                domain_id=domain.id,
                raw_url=raw_url,
                normalized_url=normalized_url,
                canonical_url=canonical_url,
                http_status=http_status,
                mime_type=mime_type,
                truncation_reason=truncation_reason,
                provenance_id=provenance_id,
                last_crawled_at=datetime.now(timezone.utc),
            )
            self.session.add(page)
            self.session.flush()
        else:
            page.last_crawled_at = datetime.now(timezone.utc)
            if http_status:
                page.http_status = http_status
            if mime_type:
                page.mime_type = mime_type
            if canonical_url:
                page.canonical_url = canonical_url
            if truncation_reason:
                page.truncation_reason = truncation_reason
            self.session.flush()
        return page

    def get_or_create_page_link(
        self,
        source_page_id: int,
        target_page_id: int,
        source_domain_id: int,
        target_domain_id: int,
        provenance_id: Optional[int] = None,
    ) -> PageLink:
        link = (
            self.session.query(PageLink)
            .filter_by(source_page_id=source_page_id, target_page_id=target_page_id)
            .first()
        )
        if not link:
            link = PageLink(
                source_page_id=source_page_id,
                target_page_id=target_page_id,
                source_domain_id=source_domain_id,
                target_domain_id=target_domain_id,
                is_active=True,
                provenance_id=provenance_id,
            )
            self.session.add(link)
            self.session.flush()
        else:
            link.last_seen_at = datetime.now(timezone.utc)
            link.is_active = True
            self.session.flush()
        return link

    def record_link_occurrence(
        self,
        page_link_id: int,
        crawl_run_id: int,
        source_id: str,
        raw_target_url: str,
        normalized_target_url: str,
        anchor_text: Optional[str] = None,
        rel_attributes: Optional[str] = None,
        link_type: str = "a_href",
        link_position: int = 0,
        dom_context: Optional[str] = None,
        http_status: Optional[int] = None,
        provenance_id: Optional[int] = None,
    ) -> LinkOccurrence:
        occ = LinkOccurrence(
            page_link_id=page_link_id,
            crawl_run_id=crawl_run_id,
            source_id=source_id,
            raw_target_url=raw_target_url,
            normalized_target_url=normalized_target_url,
            anchor_text=anchor_text,
            rel_attributes=rel_attributes,
            link_type=link_type,
            link_position=link_position,
            dom_context=dom_context,
            http_status=http_status,
            provenance_id=provenance_id,
        )
        self.session.add(occ)
        self.session.flush()
        return occ

    def upsert_webpage_metadata(
        self,
        page_id: int,
        title: Optional[str] = None,
        meta_description: Optional[str] = None,
        h1_headings: Optional[str] = None,
        h2_headings: Optional[str] = None,
        canonical_url: Optional[str] = None,
        language: Optional[str] = None,
        provenance_id: Optional[int] = None,
    ) -> WebpageMetadata:
        meta = self.session.query(WebpageMetadata).filter_by(page_id=page_id).first()
        if not meta:
            meta = WebpageMetadata(
                page_id=page_id,
                title=title,
                meta_description=meta_description,
                h1_headings=h1_headings,
                h2_headings=h2_headings,
                canonical_url=canonical_url,
                language=language,
                provenance_id=provenance_id,
            )
            self.session.add(meta)
        else:
            meta.title = title
            meta.meta_description = meta_description
            meta.h1_headings = h1_headings
            meta.h2_headings = h2_headings
            meta.canonical_url = canonical_url
            meta.language = language
            if provenance_id:
                meta.provenance_id = provenance_id
        self.session.flush()
        return meta

    def record_error(
        self,
        source_id: str,
        stage: str,
        error_code: str,
        error_message: str,
        crawl_run_id: Optional[int] = None,
        stack_trace: Optional[str] = None,
        record_locator: Optional[str] = None,
        actual_bytes: Optional[int] = None,
        provenance_id: Optional[int] = None,
    ) -> ProcessingError:
        err = ProcessingError(
            source_id=source_id,
            crawl_run_id=crawl_run_id,
            stage=stage,
            error_code=error_code,
            error_message=error_message,
            stack_trace=stack_trace,
            record_locator=record_locator,
            actual_bytes=actual_bytes,
            provenance_id=provenance_id,
        )
        self.session.add(err)
        self.session.flush()
        return err

    def upsert_domain_registration(
        self,
        domain_name: str,
        registry_domain_id: Optional[str] = None,
        registrar_name: Optional[str] = None,
        registrar_iana_id: Optional[str] = None,
        registration_status: Optional[str] = None,
        created_date: Optional[datetime] = None,
        updated_date: Optional[datetime] = None,
        expiration_date: Optional[datetime] = None,
        nameservers: Optional[str] = None,
        registrant_organization: Optional[str] = None,
        registrant_country: Optional[str] = None,
        is_privacy_redacted: bool = False,
        raw_rdap_json: Optional[str] = None,
        provenance_id: Optional[int] = None,
    ) -> DomainRegistrationData:
        domain = self.get_or_create_domain(domain_name)
        reg = self.session.query(DomainRegistrationData).filter_by(domain_id=domain.id).first()
        now = datetime.now(timezone.utc)
        if not reg:
            reg = DomainRegistrationData(
                domain_id=domain.id,
                registry_domain_id=registry_domain_id,
                registrar_name=registrar_name,
                registrar_iana_id=registrar_iana_id,
                registration_status=registration_status,
                created_date=created_date,
                updated_date=updated_date,
                expiration_date=expiration_date,
                nameservers=nameservers,
                registrant_organization=registrant_organization,
                registrant_country=registrant_country,
                is_privacy_redacted=is_privacy_redacted,
                raw_rdap_json=raw_rdap_json,
                last_checked_at=now,
                provenance_id=provenance_id,
            )
            self.session.add(reg)
        else:
            reg.registry_domain_id = registry_domain_id
            reg.registrar_name = registrar_name
            reg.registrar_iana_id = registrar_iana_id
            reg.registration_status = registration_status
            reg.created_date = created_date
            reg.updated_date = updated_date
            reg.expiration_date = expiration_date
            reg.nameservers = nameservers
            reg.registrant_organization = registrant_organization
            reg.registrant_country = registrant_country
            reg.is_privacy_redacted = is_privacy_redacted
            reg.raw_rdap_json = raw_rdap_json
            reg.last_checked_at = now
            if provenance_id:
                reg.provenance_id = provenance_id
        self.session.flush()
        return reg

    def get_domain_registration(self, domain_name: str) -> Optional[DomainRegistrationData]:
        domain = self.session.query(Domain).filter_by(domain_name=domain_name.strip().lower()).first()
        if not domain:
            return None
        return self.session.query(DomainRegistrationData).filter_by(domain_id=domain.id).first()

    def upsert_historical_snapshot(
        self,
        domain_name: str,
        page_id: int,
        timestamp: str,
        snapshot_url: str,
        original_url: str,
        http_status: Optional[int] = None,
        mime_type: Optional[str] = None,
        digest: Optional[str] = None,
        content_sha256: str = "",
        downloaded_bytes: int = 0,
        title: Optional[str] = None,
        meta_description: Optional[str] = None,
        outbound_links_count: int = 0,
        captured_at: Optional[datetime] = None,
        crawl_run_id: Optional[int] = None,
        provenance_id: Optional[int] = None,
    ) -> HistoricalSnapshot:
        domain = self.get_or_create_domain(domain_name)
        snap = (
            self.session.query(HistoricalSnapshot)
            .filter_by(page_id=page_id, timestamp=timestamp)
            .first()
        )
        if not snap:
            snap = HistoricalSnapshot(
                domain_id=domain.id,
                page_id=page_id,
                crawl_run_id=crawl_run_id,
                timestamp=timestamp,
                snapshot_url=snapshot_url,
                original_url=original_url,
                http_status=http_status,
                mime_type=mime_type,
                digest=digest,
                content_sha256=content_sha256,
                downloaded_bytes=downloaded_bytes,
                title=title,
                meta_description=meta_description,
                outbound_links_count=outbound_links_count,
                captured_at=captured_at,
                provenance_id=provenance_id,
            )
            self.session.add(snap)
        else:
            snap.snapshot_url = snapshot_url
            snap.original_url = original_url
            snap.http_status = http_status
            snap.mime_type = mime_type
            snap.digest = digest
            snap.content_sha256 = content_sha256
            snap.downloaded_bytes = downloaded_bytes
            snap.title = title
            snap.meta_description = meta_description
            snap.outbound_links_count = outbound_links_count
            snap.captured_at = captured_at
            if provenance_id:
                snap.provenance_id = provenance_id
        self.session.flush()
        return snap

    def get_historical_snapshots(self, domain_name: str) -> List[HistoricalSnapshot]:
        domain = self.session.query(Domain).filter_by(domain_name=domain_name.strip().lower()).first()
        if not domain:
            return []
        return (
            self.session.query(HistoricalSnapshot)
            .filter_by(domain_id=domain.id)
            .order_by(HistoricalSnapshot.timestamp.asc())
            .all()
        )

    def save_credential(
        self,
        source_id: str,
        credential_type: str,
        plaintext_secret: str,
        account_identifier: Optional[str] = None,
    ) -> SourceCredential:
        encrypted = encrypt_secret(plaintext_secret)
        now = datetime.now(timezone.utc)
        cred = self.session.query(SourceCredential).filter_by(source_id=source_id, credential_type=credential_type).first()
        if not cred:
            cred = SourceCredential(
                source_id=source_id,
                credential_type=credential_type,
                account_identifier=account_identifier,
                encrypted_secret=encrypted,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            self.session.add(cred)
        else:
            cred.encrypted_secret = encrypted
            cred.account_identifier = account_identifier
            cred.is_active = True
            cred.updated_at = now
        self.session.flush()
        return cred

    def get_credential(self, source_id: str, credential_type: Optional[str] = None) -> Optional[str]:
        q = self.session.query(SourceCredential).filter_by(source_id=source_id, is_active=True)
        if credential_type:
            q = q.filter_by(credential_type=credential_type)
        cred = q.first()
        if not cred:
            return None
        return decrypt_secret(cred.encrypted_secret)

    def record_sync_history(
        self,
        source_id: str,
        operation_name: str,
        status: str,
        crawl_run_id: Optional[int] = None,
        items_count: int = 0,
        http_status: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> SourceSyncHistory:
        now = datetime.now(timezone.utc)
        sync = SourceSyncHistory(
            source_id=source_id,
            crawl_run_id=crawl_run_id,
            operation_name=operation_name,
            status=status,
            items_count=items_count,
            http_status=http_status,
            error_message=error_message,
            sync_started_at=now,
            sync_completed_at=now,
        )
        self.session.add(sync)
        self.session.flush()
        return sync

    def upsert_page_authority_scores(
        self,
        records: List[Dict[str, Any]],
        target_domain: Optional[str] = None,
    ) -> int:
        """
        Atomically replaces page authority scores for target_domain (or globally).
        Executes within self.session. If any insertion fails, transaction rolls back.
        """
        # Step 1: Scope-bounded deletion
        if target_domain is not None:
            self.session.query(PageAuthorityScore).filter(
                PageAuthorityScore.domain == target_domain
            ).delete(synchronize_session=False)
        else:
            self.session.query(PageAuthorityScore).delete(synchronize_session=False)

        # Step 2: Batch insertion
        if records:
            models = [PageAuthorityScore(**rec) for rec in records]
            self.session.add_all(models)
            self.session.flush()

        return len(records)

    def get_authority_scores(
        self,
        domain: Optional[str] = None,
        min_score: float = 0.0,
        limit: int = 100,
        internal_only: bool = True,
    ) -> List[PageAuthorityScore]:
        """Query persisted page authority scores with optional filters."""
        q = self.session.query(PageAuthorityScore)
        if domain is not None:
            q = q.filter(PageAuthorityScore.domain == domain)
        if min_score > 0.0:
            q = q.filter(PageAuthorityScore.lp_pa_score >= min_score)
        if internal_only:
            q = q.filter(PageAuthorityScore.is_internal_page.is_(True))
        return q.order_by(PageAuthorityScore.lp_pa_score.desc()).limit(limit).all()

    def get_top_authority_scores(
        self,
        limit: int = 10,
        internal_only: bool = True,
    ) -> List[PageAuthorityScore]:
        """Return top-N pages by LP-PA score across all domains."""
        q = self.session.query(PageAuthorityScore)
        if internal_only:
            q = q.filter(PageAuthorityScore.is_internal_page.is_(True))
        return q.order_by(PageAuthorityScore.lp_pa_score.desc()).limit(limit).all()

    def resolve_domain_id(self, site_boundary: str) -> Optional[int]:
        """
        Deterministically resolve site_boundary to domains.id.
        Priority: 1) exact match on Domain.domain_name, 2) match on Domain.registered_domain.
        Tie-breaker: Domain.id ASC. Returns None if no matching Domain exists.
        """
        if not site_boundary:
            return None
        domain_rec = (
            self.session.query(Domain)
            .filter(
                (Domain.domain_name == site_boundary)
                | (Domain.registered_domain == site_boundary)
            )
            .order_by(
                case((Domain.domain_name == site_boundary, 0), else_=1),
                Domain.id.asc(),
            )
            .first()
        )
        return domain_rec.id if domain_rec is not None else None

    def upsert_domain_authority_scores(
        self,
        records: List[Dict[str, Any]],
        target_domain: Optional[str] = None,
    ) -> int:
        """
        Atomically replaces domain authority scores for target_domain (or globally).
        Executes within self.session without committing or closing the session.
        """
        if target_domain is not None:
            self.session.query(DomainAuthorityScore).filter(
                DomainAuthorityScore.domain_name == target_domain
            ).delete(synchronize_session=False)
        else:
            self.session.query(DomainAuthorityScore).delete(synchronize_session=False)

        if records:
            models = []
            for rec in records:
                payload = dict(rec)
                if payload.get("domain_id") is None and payload.get("domain_name"):
                    payload["domain_id"] = self.resolve_domain_id(payload["domain_name"])
                models.append(DomainAuthorityScore(**payload))
            self.session.add_all(models)
            self.session.flush()

        return len(records)

    def get_domain_authority_score(
        self,
        domain_name: str,
    ) -> Optional[DomainAuthorityScore]:
        """
        Retrieve persisted DomainAuthorityScore for a domain.
        Canonicalizes raw input through LinkPilot's PSL normalizer before querying.
        """
        if not domain_name or not isinstance(domain_name, str) or not domain_name.strip():
            return None
        from app.authority.domain_scorer import canonicalize_domain_lookup
        try:
            canonical_name = canonicalize_domain_lookup(domain_name)
        except ValueError:
            canonical_name = domain_name.strip().lower().rstrip(".")

        return (
            self.session.query(DomainAuthorityScore)
            .filter(DomainAuthorityScore.domain_name == canonical_name)
            .first()
        )

    def get_top_domain_authority_scores(
        self,
        limit: int = 50,
        min_score: float = 0.0,
    ) -> List[DomainAuthorityScore]:
        """Retrieve top domains ordered by LP-DA score descending, then domain_name ascending."""
        q = self.session.query(DomainAuthorityScore)
        if min_score > 0.0:
            q = q.filter(DomainAuthorityScore.lp_da_score >= min_score)
        return (
            q.order_by(
                DomainAuthorityScore.lp_da_score.desc(),
                DomainAuthorityScore.domain_name.asc(),
            )
            .limit(limit)
            .all()
        )





