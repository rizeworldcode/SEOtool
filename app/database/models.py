"""
SQLAlchemy ORM models for LinkPilot Data Engine (Milestone 0 & 1A Essential Models).
Supports PostgreSQL and SQLite with complete relational constraints, indexes,
and strict data provenance tracking.
"""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    DateTime,
    Float,
    Boolean,
    ForeignKey,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class DataProvenance(Base):
    """Master provenance audit trail linking records back to exact source locators."""
    __tablename__ = "data_provenance"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(String(50), nullable=False, index=True)  # commoncrawl, bing, gsc_api, gsc_export, wayback, rdap
    dataset_identifier = Column(String(100), nullable=False, index=True)  # e.g. CC-MAIN-2024-51
    record_locator = Column(Text, nullable=False)  # filename, offset, length, or API query endpoint
    warc_record_id = Column(String(255), nullable=True)  # WARC-Record-ID urn:uuid:...
    warc_type = Column(String(50), nullable=True)  # response, warcinfo, metadata
    http_status = Column(Integer, nullable=True)
    content_type = Column(String(100), nullable=True)
    warc_truncated = Column(String(50), nullable=True)  # length, time, disconnect
    downloaded_bytes = Column(BigInteger, nullable=True)
    record_compressed_sha256 = Column(String(64), nullable=True)
    record_decompressed_sha256 = Column(String(64), nullable=True)
    payload_sha256 = Column(String(64), nullable=True)
    warc_payload_digest = Column(String(100), nullable=True)  # Raw sha1:BASE32 from CC
    retrieval_timestamp = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index("idx_provenance_source_dataset", "source_id", "dataset_identifier"),
    )


class DataSource(Base):
    """Registry of external data sources and their health / config status."""
    __tablename__ = "data_sources"

    id = Column(String(50), primary_key=True)  # commoncrawl, bing, gsc_api, gsc_export, wayback, rdap
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    is_enabled = Column(Boolean, default=True, nullable=False)
    health_status = Column(String(50), default="unknown", nullable=False)  # healthy, degraded, unavailable
    last_health_check = Column(DateTime(timezone=True), nullable=True)
    rate_limit_rps = Column(Float, default=1.0, nullable=False)
    concurrency_limit = Column(Integer, default=5, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class CrawlCollection(Base):
    """Common Crawl releases registry (e.g. CC-MAIN-2024-51)."""
    __tablename__ = "crawl_collections"

    id = Column(String(50), primary_key=True)  # e.g. CC-MAIN-2024-51
    name = Column(String(100), nullable=False)
    timegate_url = Column(Text, nullable=True)
    cdx_api_url = Column(Text, nullable=False)
    collinfo_json = Column(Text, nullable=True)
    from_timestamp = Column(DateTime(timezone=True), nullable=True)
    to_timestamp = Column(DateTime(timezone=True), nullable=True)
    is_available = Column(Boolean, default=True, nullable=False)
    discovered_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class CrawlRun(Base):
    """Represents a discrete execution run or ingestion batch."""
    __tablename__ = "crawl_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(String(50), ForeignKey("data_sources.id"), nullable=False, index=True)
    run_type = Column(String(50), nullable=False)  # cc_targeted_outbound, cc_webgraph_benchmark, etc.
    target_domain = Column(String(255), nullable=True, index=True)
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(50), default="running", nullable=False)  # running, completed, failed, paused
    bytes_downloaded = Column(BigInteger, default=0, nullable=False)
    records_discovered = Column(Integer, default=0, nullable=False)
    records_ingested = Column(Integer, default=0, nullable=False)
    errors_count = Column(Integer, default=0, nullable=False)
    provenance_id = Column(Integer, ForeignKey("data_provenance.id"), nullable=True)

    provenance = relationship("DataProvenance")


class DownloadJob(Base):
    """Resumable download job state tracking."""
    __tablename__ = "download_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    crawl_run_id = Column(Integer, ForeignKey("crawl_runs.id"), nullable=False, index=True)
    source_url = Column(Text, nullable=False)
    destination_path = Column(Text, nullable=False)
    expected_bytes = Column(BigInteger, nullable=True)
    downloaded_bytes = Column(BigInteger, default=0, nullable=False)
    status = Column(String(50), default="pending", nullable=False)  # pending, running, completed, failed, paused
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class DownloadFile(Base):
    """Physical file tracker with SHA-256 and byte counts."""
    __tablename__ = "download_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    crawl_run_id = Column(Integer, ForeignKey("crawl_runs.id"), nullable=True, index=True)
    file_path = Column(Text, nullable=False, unique=True)
    file_size_bytes = Column(BigInteger, nullable=False)
    sha256_checksum = Column(String(64), nullable=False, index=True)
    mime_type = Column(String(100), nullable=True)
    is_compressed = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class Domain(Base):
    """Canonical domain registry."""
    __tablename__ = "domains"

    id = Column(Integer, primary_key=True, autoincrement=True)
    domain_name = Column(String(255), nullable=False, unique=True, index=True)  # e.g. zoniraz.com
    registered_domain = Column(String(255), nullable=False, index=True)  # e.g. zoniraz.com
    tld = Column(String(50), nullable=False, index=True)  # e.g. com
    is_punycode = Column(Boolean, default=False, nullable=False)
    first_discovered_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class Page(Base):
    """Canonical page registry."""
    __tablename__ = "pages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    domain_id = Column(Integer, ForeignKey("domains.id"), nullable=False, index=True)
    raw_url = Column(Text, nullable=False)
    normalized_url = Column(Text, nullable=False, unique=True, index=True)
    canonical_url = Column(Text, nullable=True)
    http_status = Column(Integer, nullable=True)
    mime_type = Column(String(100), nullable=True)
    truncation_reason = Column(String(50), nullable=True)  # length, time, disconnect
    first_discovered_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_crawled_at = Column(DateTime(timezone=True), nullable=True)
    provenance_id = Column(Integer, ForeignKey("data_provenance.id"), nullable=True)

    domain = relationship("Domain")
    provenance = relationship("DataProvenance")


class PageLink(Base):
    """Canonical directed page-to-page link relationship."""
    __tablename__ = "page_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_page_id = Column(Integer, ForeignKey("pages.id"), nullable=False, index=True)
    target_page_id = Column(Integer, ForeignKey("pages.id"), nullable=False, index=True)
    source_domain_id = Column(Integer, ForeignKey("domains.id"), nullable=False, index=True)
    target_domain_id = Column(Integer, ForeignKey("domains.id"), nullable=False, index=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    is_active = Column(Boolean, default=True, nullable=False)
    provenance_id = Column(Integer, ForeignKey("data_provenance.id"), nullable=True)

    source_page = relationship("Page", foreign_keys=[source_page_id])
    target_page = relationship("Page", foreign_keys=[target_page_id])
    provenance = relationship("DataProvenance")

    __table_args__ = (
        UniqueConstraint("source_page_id", "target_page_id", name="uq_page_links_source_target"),
        Index("idx_page_links_source_target", "source_page_id", "target_page_id"),
        Index("idx_page_links_domains", "source_domain_id", "target_domain_id"),
    )


class LinkOccurrence(Base):
    """Captures every individual physical link occurrence on a source page."""
    __tablename__ = "link_occurrences"

    id = Column(Integer, primary_key=True, autoincrement=True)
    page_link_id = Column(Integer, ForeignKey("page_links.id"), nullable=False, index=True)
    crawl_run_id = Column(Integer, ForeignKey("crawl_runs.id"), nullable=False, index=True)
    observed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    source_id = Column(String(50), nullable=False, index=True)
    raw_target_url = Column(Text, nullable=False)
    normalized_target_url = Column(Text, nullable=False)
    anchor_text = Column(Text, nullable=True)
    rel_attributes = Column(String(100), nullable=True)  # nofollow, sponsored, ugc
    link_type = Column(String(50), default="a_href", nullable=False)  # a_href, image_anchor, area_href
    link_position = Column(Integer, default=0, nullable=False)  # 0-indexed DOM order
    dom_context = Column(String(100), nullable=True)  # header, nav, main, footer, article
    http_status = Column(Integer, nullable=True)
    raw_payload_ref = Column(Text, nullable=True)
    provenance_id = Column(Integer, ForeignKey("data_provenance.id"), nullable=True)

    page_link = relationship("PageLink")
    crawl_run = relationship("CrawlRun")
    provenance = relationship("DataProvenance")

    __table_args__ = (
        Index("idx_link_occ_run_link", "crawl_run_id", "page_link_id"),
    )


class WebpageMetadata(Base):
    """Metadata extracted from HTML (title, description, headings, canonical)."""
    __tablename__ = "webpage_metadata"

    id = Column(Integer, primary_key=True, autoincrement=True)
    page_id = Column(Integer, ForeignKey("pages.id"), nullable=False, unique=True, index=True)
    title = Column(Text, nullable=True)
    meta_description = Column(Text, nullable=True)
    h1_headings = Column(Text, nullable=True)  # JSON or newline-separated
    h2_headings = Column(Text, nullable=True)  # JSON or newline-separated
    canonical_url = Column(Text, nullable=True)
    language = Column(String(20), nullable=True)
    provenance_id = Column(Integer, ForeignKey("data_provenance.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    page = relationship("Page")
    provenance = relationship("DataProvenance")


class ProcessingError(Base):
    """Detailed error and rejection logging for malformed or oversized records."""
    __tablename__ = "processing_errors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(String(50), nullable=False, index=True)
    crawl_run_id = Column(Integer, ForeignKey("crawl_runs.id"), nullable=True, index=True)
    stage = Column(String(50), nullable=False)  # discovery, download, warc_decompression, html_parse, db_insert
    error_code = Column(String(100), nullable=False)  # e.g. EXCEEDED_COMPRESSED_LIMIT, MALFORMED_WARC
    error_message = Column(Text, nullable=False)
    stack_trace = Column(Text, nullable=True)
    record_locator = Column(Text, nullable=True)
    actual_bytes = Column(BigInteger, nullable=True)
    provenance_id = Column(Integer, ForeignKey("data_provenance.id"), nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class DomainRegistrationData(Base):
    """Normalized domain registration records from ICANN / RDAP."""
    __tablename__ = "domain_registration_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    domain_id = Column(Integer, ForeignKey("domains.id"), nullable=False, unique=True, index=True)
    registry_domain_id = Column(String(100), nullable=True)
    registrar_name = Column(String(255), nullable=True)
    registrar_iana_id = Column(String(50), nullable=True)
    registration_status = Column(Text, nullable=True)  # JSON array of status strings
    created_date = Column(DateTime(timezone=True), nullable=True)
    updated_date = Column(DateTime(timezone=True), nullable=True)
    expiration_date = Column(DateTime(timezone=True), nullable=True)
    nameservers = Column(Text, nullable=True)  # JSON array of nameserver strings
    registrant_organization = Column(String(255), nullable=True)
    registrant_country = Column(String(10), nullable=True)
    is_privacy_redacted = Column(Boolean, default=False, nullable=False)
    raw_rdap_json = Column(Text, nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    provenance_id = Column(Integer, ForeignKey("data_provenance.id"), nullable=True)

    domain = relationship("Domain")
    provenance = relationship("DataProvenance")


class HistoricalSnapshot(Base):
    """Historical archive snapshot of a page from Wayback Machine."""
    __tablename__ = "historical_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    domain_id = Column(Integer, ForeignKey("domains.id"), nullable=False, index=True)
    page_id = Column(Integer, ForeignKey("pages.id"), nullable=False, index=True)
    crawl_run_id = Column(Integer, ForeignKey("crawl_runs.id"), nullable=True, index=True)
    timestamp = Column(String(14), nullable=False, index=True)  # e.g. 20230514120000
    snapshot_url = Column(Text, nullable=False)  # Raw unmodified id_ URL
    original_url = Column(Text, nullable=False)
    http_status = Column(Integer, nullable=True)
    mime_type = Column(String(100), nullable=True)
    digest = Column(String(100), nullable=True)  # Wayback digest
    content_sha256 = Column(String(64), nullable=False)
    downloaded_bytes = Column(BigInteger, nullable=False)
    title = Column(Text, nullable=True)
    meta_description = Column(Text, nullable=True)
    outbound_links_count = Column(Integer, default=0, nullable=False)
    captured_at = Column(DateTime(timezone=True), nullable=True)
    ingested_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    provenance_id = Column(Integer, ForeignKey("data_provenance.id"), nullable=True)

    domain = relationship("Domain")
    page = relationship("Page")
    provenance = relationship("DataProvenance")

    __table_args__ = (
        UniqueConstraint("page_id", "timestamp", name="uq_page_snapshot_timestamp"),
    )


class SourceCredential(Base):
    """Encrypted API keys and OAuth tokens for external sources (Bing, GSC)."""
    __tablename__ = "source_credentials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(String(50), nullable=False, index=True)  # bing, google_search_console
    credential_type = Column(String(50), nullable=False)  # api_key, oauth2_token, service_account
    account_identifier = Column(String(255), nullable=True)  # email, client_id, user
    encrypted_secret = Column(Text, nullable=False)  # AES-256-GCM ciphertext
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class SourceSyncHistory(Base):
    """Audit log of remote API sync operations."""
    __tablename__ = "source_sync_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(String(50), nullable=False, index=True)
    crawl_run_id = Column(Integer, ForeignKey("crawl_runs.id"), nullable=True, index=True)
    operation_name = Column(String(100), nullable=False)  # GetUserSites, GetLinkCounts, GetUrlLinks, searchAnalytics
    status = Column(String(50), nullable=False)  # success, failed, auth_error, unverified
    items_count = Column(Integer, default=0, nullable=False)
    http_status = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    sync_started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    sync_completed_at = Column(DateTime(timezone=True), nullable=True)


class PageAuthorityScore(Base):
    """
    Authoritative Page Authority (LP-PA) scores computed by Phase 1B Milestone 4.
    Stores component-scaled PageRank and final logarithmic equity scores.
    """
    __tablename__ = "page_authority_scores"

    id = Column(Integer, primary_key=True, autoincrement=True)
    page_id = Column(Integer, ForeignKey("pages.id", ondelete="SET NULL"), nullable=True, index=True)
    normalized_url = Column(Text, nullable=False, unique=True, index=True)
    domain = Column(Text, nullable=False, index=True)
    is_internal_page = Column(Boolean, nullable=False, default=True, index=True)
    lp_pa_score = Column(Float, nullable=False)
    raw_pagerank = Column(Float, nullable=False)
    component_id = Column(Integer, nullable=False)
    comp_scale = Column(Float, nullable=False)
    alpha = Column(Float, nullable=False)
    pagerank_status = Column(String(50), nullable=False)
    iterations = Column(Integer, nullable=False)
    final_residual = Column(Float, nullable=False)
    computed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    page = relationship("Page", foreign_keys=[page_id])

    __table_args__ = (
        UniqueConstraint("normalized_url", name="uq_page_authority_scores_url"),
        Index("ix_pas_score_desc", "lp_pa_score"),
        Index("ix_pas_domain_score", "domain", "lp_pa_score"),
    )


class DomainAuthorityScore(Base):
    """
    Authoritative Domain Authority (LP-DA) scores computed by Phase 1B Milestone 5.
    Stores multi-component aggregated domain authority derived from Page Authority.
    """
    __tablename__ = "domain_authority_scores"

    id = Column(Integer, primary_key=True, autoincrement=True)
    domain_id = Column(Integer, ForeignKey("domains.id", ondelete="SET NULL"), nullable=True, index=True)
    domain_name = Column(String(255), nullable=False, unique=True)
    lp_da_score = Column(Float, nullable=False)
    lp_da_display = Column(Float, nullable=False)
    anchor_page_pa = Column(Float, nullable=False)
    top_k_harmonic_pa = Column(Float, nullable=False)
    mean_page_pa = Column(Float, nullable=False)
    breadth_score = Column(Float, nullable=False)
    scored_pages_count = Column(Integer, nullable=False)
    configured_top_k = Column(Integer, nullable=False, default=5)
    harmonic_epsilon = Column(Float, nullable=False, default=1e-4)
    reference_page_count = Column(Float, nullable=False, default=100.0)
    alpha = Column(Float, nullable=False, default=0.40)
    beta = Column(Float, nullable=False, default=0.35)
    gamma = Column(Float, nullable=False, default=0.25)
    algorithm_version = Column(String(50), nullable=False, default="LP-DA-v1.0.0")
    computed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    domain_rel = relationship("Domain", foreign_keys=[domain_id])

    __table_args__ = (
        Index("ix_das_score_desc", "lp_da_score"),
    )





