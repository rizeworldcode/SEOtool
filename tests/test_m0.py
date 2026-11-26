"""
Unit tests for Milestone 0: Core Foundation, Models, Repositories, and SSRF Guard.
"""
import pytest
from app.database.connection import init_db, get_db
from app.database.models import DataSource, Page, PageLink, LinkOccurrence
from app.database.repositories import CrawlRepository
from app.security.ssrf_guard import validate_url_safety, SSRFSecurityViolation, is_ip_blocked
from app.security.crypto import encrypt_secret, decrypt_secret


def test_ssrf_blocked_ips():
    """Verify private/loopback/cloud-metadata IPs are blocked."""
    blocked, reason = is_ip_blocked("127.0.0.1")
    assert blocked is True
    assert "restricted network" in reason

    blocked, reason = is_ip_blocked("169.254.169.254")
    assert blocked is True

    blocked, reason = is_ip_blocked("10.0.0.1")
    assert blocked is True

    blocked, reason = is_ip_blocked("192.168.1.1")
    assert blocked is True

    # IPv6 Loopback
    blocked, reason = is_ip_blocked("::1")
    assert blocked is True

    # IPv4-mapped IPv6 loopback
    blocked, reason = is_ip_blocked("::ffff:127.0.0.1")
    assert blocked is True

    # Public IP
    blocked, reason = is_ip_blocked("93.184.216.34")  # example.com
    assert blocked is False


def test_crypto_roundtrip():
    """Verify AES-256-GCM encryption and decryption."""
    secret = "my-super-secret-oauth-token-12345"
    encrypted = encrypt_secret(secret)
    assert encrypted != secret
    decrypted = decrypt_secret(encrypted)
    assert decrypted == secret


def test_database_init_and_repository():
    """Verify database initialization and repository models."""
    init_db()
    with get_db() as session:
        repo = CrawlRepository(session)
        source = repo.get_or_create_source("commoncrawl", "Common Crawl")
        assert source.id == "commoncrawl"

        prov = repo.record_provenance(
            source_id="commoncrawl",
            dataset_identifier="CC-MAIN-TEST",
            record_locator="test.warc.gz,0,1000",
            downloaded_bytes=1000,
            record_compressed_sha256="abc123",
            payload_sha256="def456",
        )
        assert prov.id is not None

        run = repo.create_crawl_run("commoncrawl", "test_run", "example.com")
        assert run.id is not None

        # Create source and target pages
        p1 = repo.get_or_create_page("https://example.com/source", "https://example.com/source", 200, "text/html", provenance_id=prov.id)
        p2 = repo.get_or_create_page("https://example.com/target", "https://example.com/target", 200, "text/html", provenance_id=prov.id)

        # Create page link
        link = repo.get_or_create_page_link(p1.id, p2.id, p1.domain_id, p2.domain_id, provenance_id=prov.id)
        assert link.id is not None

        # Record multiple link occurrences on the same page
        occ1 = repo.record_link_occurrence(
            page_link_id=link.id,
            crawl_run_id=run.id,
            source_id="commoncrawl",
            raw_target_url="https://example.com/target",
            normalized_target_url="https://example.com/target",
            anchor_text="Click here",
            link_position=1,
            dom_context="header",
            provenance_id=prov.id,
        )
        assert occ1.id is not None

        occ2 = repo.record_link_occurrence(
            page_link_id=link.id,
            crawl_run_id=run.id,
            source_id="commoncrawl",
            raw_target_url="https://example.com/target#footer",
            normalized_target_url="https://example.com/target",
            anchor_text="Learn more",
            link_position=15,
            dom_context="footer",
            provenance_id=prov.id,
        )
        assert occ2.id is not None
        assert occ1.id != occ2.id
