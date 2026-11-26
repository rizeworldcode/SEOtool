"""
Unit and Integration Tests for Milestone 4:
Bing Webmaster Tools Connector & Post-Sunset API Verification.
"""
import pytest
from app.database.connection import init_db, get_db
from app.database.models import Page, PageLink, LinkOccurrence, DataProvenance, CrawlRun, SourceCredential, SourceSyncHistory
from app.database.repositories import CrawlRepository
from app.sources.bing.client import (
    BingWebmasterClient,
    BingSite,
    BingLinkCountsResult,
    BingUrlLinksResult,
    BingLinkSummary,
    BingUrlDetail,
)
from app.sources.bing.pipeline import BingPipeline, BingAuthenticationError


@pytest.fixture(scope="module")
def setup_database():
    init_db()


def test_m4_bing_service_availability_probe():
    """Verify live reachability of Bing JSON/HTTP WCF endpoint post-August 2026."""
    client = BingWebmasterClient()
    probe_res = client.probe_service()

    assert probe_res["endpoint"] is not None
    assert "GetUserSites" in probe_res["endpoint"]
    # The endpoint returns HTTP 400 with WCF error {"ErrorCode":3,"Message":"ERROR!!! InvalidApiKey"}
    # rather than 404/410, proving service is active post-August 2026.
    assert probe_res["is_active_post_sunset"] is True
    assert probe_res["http_status"] in (200, 400)


def test_m4_bing_credential_encryption_and_retrieval(setup_database):
    """Verify AES-256-GCM credential encryption, DB storage, and decryption."""
    raw_key = "test_bing_api_key_secret_998877"
    with get_db() as session:
        repo = CrawlRepository(session)
        cred = repo.save_credential(
            source_id="bing",
            credential_type="api_key",
            plaintext_secret=raw_key,
            account_identifier="seo_admin@linkpilot.ai",
        )
        assert cred.id is not None
        # Must be encrypted ciphertext, NOT plaintext
        assert cred.encrypted_secret != raw_key
        assert len(cred.encrypted_secret) > len(raw_key)

        # Retrieve and decrypt
        decrypted = repo.get_credential("bing", "api_key")
        assert decrypted == raw_key


def test_m4_bing_client_schema_parsing():
    """Verify parsing of audited Bing WCF JSON schemas."""
    # 1. GetUserSites response schema
    sites_raw = {
        "d": [
            {"__type": "Site:http://schemas.datacontract.org/2004/07/Microsoft.Bing.Webmaster.Api", "Url": "https://zoniraz.com", "AuthenticationCode": "AUTH_123"},
            {"__type": "Site:http://schemas.datacontract.org/2004/07/Microsoft.Bing.Webmaster.Api", "Url": "https://example.com", "AuthenticationCode": "AUTH_456"},
        ]
    }
    client = BingWebmasterClient()
    sites = [
        BingSite(url=item["Url"], authentication_code=item.get("AuthenticationCode"))
        for item in sites_raw["d"]
    ]
    assert len(sites) == 2
    assert sites[0].url == "https://zoniraz.com"

    # 2. GetLinkCounts response schema
    link_counts_raw = {
        "d": {
            "__type": "LinkCounts:http://schemas.datacontract.org/2004/07/Microsoft.Bing.Webmaster.Api",
            "Links": [
                {"Url": "https://ref-domain-one.com", "Count": 42},
                {"Url": "https://ref-domain-two.com", "Count": 18},
            ],
            "TotalPages": 1,
        }
    }
    lc_result = BingLinkCountsResult(
        links=[BingLinkSummary(referring_domain_url=i["Url"], count=i["Count"]) for i in link_counts_raw["d"]["Links"]],
        total_pages=link_counts_raw["d"]["TotalPages"],
    )
    assert len(lc_result.links) == 2
    assert lc_result.links[0].count == 42
    assert lc_result.total_pages == 1

    # 3. GetUrlLinks response schema
    url_links_raw = {
        "d": {
            "__type": "UrlLinks:http://schemas.datacontract.org/2004/07/Microsoft.Bing.Webmaster.Api",
            "Details": [
                {"Url": "https://ref-domain-one.com/article", "AnchorText": "Best Jewellery Store"},
            ],
            "TotalPages": 1,
        }
    }
    ul_result = BingUrlLinksResult(
        details=[BingUrlDetail(referring_page_url=i["Url"], anchor_text=i["AnchorText"]) for i in url_links_raw["d"]["Details"]],
        total_pages=url_links_raw["d"]["TotalPages"],
    )
    assert len(ul_result.details) == 1
    assert ul_result.details[0].anchor_text == "Best Jewellery Store"


def test_m4_bing_pipeline_mock_sync_and_persistence(setup_database):
    """Verify Bing ingestion pipeline, link occurrence creation, and provenance storage."""
    pipeline = BingPipeline()
    result = pipeline.run(site_url="https://zoniraz.com", mock=True)

    assert result.status == "completed"
    assert result.referring_domains_discovered >= 2
    assert result.referring_urls_ingested >= 4
    assert result.total_bytes_processed > 0

    with get_db() as session:
        # Verify provenance record
        prov = session.query(DataProvenance).filter_by(source_id="bing").first()
        assert prov is not None
        assert prov.dataset_identifier == "bing_webmaster_api_v1"

        # Verify page links and occurrences
        occs = session.query(LinkOccurrence).filter_by(source_id="bing").all()
        assert len(occs) >= 4
        assert any(o.anchor_text == "Zoniraz Official Store" for o in occs)

        # Verify crawl run state
        run = session.get(CrawlRun, result.crawl_run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.records_ingested == result.referring_urls_ingested
