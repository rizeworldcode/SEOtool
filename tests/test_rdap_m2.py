"""
Unit and Integration Tests for Milestone 2:
ICANN / RDAP Domain Registration Ingestion Pipeline.
"""
import pytest
from datetime import datetime
from app.database.connection import init_db, get_db
from app.database.models import Domain, DomainRegistrationData, DataProvenance, CrawlRun
from app.sources.rdap.bootstrap import RDAPBootstrapResolver
from app.sources.rdap.client import RDAPClient, RDAPDomainRecord
from app.sources.rdap.pipeline import RDAPPipeline


@pytest.fixture(scope="module")
def setup_database():
    init_db()


def test_m2_iana_bootstrap_resolution():
    """Verify IANA bootstrap accurately resolves authoritative TLD endpoints."""
    resolver = RDAPBootstrapResolver()
    com_endpoint = resolver.resolve_tld_endpoint("com")
    org_endpoint = resolver.resolve_tld_endpoint("org")

    assert com_endpoint is not None
    assert "verisign.com" in com_endpoint
    assert org_endpoint is not None
    assert "publicinterestregistry.org" in org_endpoint


def test_m2_rdap_parsing_and_privacy_detection():
    """Verify RFC 7483 parser extracts registrar, dates, nameservers, and redaction flags."""
    sample_rdap = {
        "handle": "12345_DOMAIN_COM-VRSN",
        "status": ["clientTransferProhibited", "clientDeleteProhibited"],
        "events": [
            {"eventAction": "registration", "eventDate": "2020-01-15T10:00:00Z"},
            {"eventAction": "expiration", "eventDate": "2025-01-15T10:00:00Z"},
            {"eventAction": "last changed", "eventDate": "2023-05-20T12:00:00Z"},
        ],
        "nameservers": [
            {"ldhName": "ns1.exampledns.com"},
            {"ldhName": "ns2.exampledns.com"},
        ],
        "entities": [
            {
                "roles": ["registrar"],
                "vcardArray": ["vcard", [["version", {}, "text", "4.0"], ["fn", {}, "text", "Test Registrar Inc."]]],
                "publicIds": [{"type": "IANA Registrar ID", "identifier": "999"}],
            },
            {
                "roles": ["registrant"],
                "remarks": [{"description": ["Registrant personal details are redacted for privacy."]}],
                "vcardArray": ["vcard", [["version", {}, "text", "4.0"], ["fn", {}, "text", "REDACTED FOR PRIVACY"]]],
            },
        ],
        "notices": [
            {"title": "GDPR Notice", "description": ["Personal data is withheld in accordance with GDPR."]}
        ],
    }

    client = RDAPClient()
    record: RDAPDomainRecord = client._parse_rdap_response("example.com", "https://rdap.example.com/domain/example.com", sample_rdap)

    assert record.domain_name == "example.com"
    assert record.registry_domain_id == "12345_DOMAIN_COM-VRSN"
    assert record.registrar_name == "Test Registrar Inc."
    assert record.registrar_iana_id == "999"
    assert "clientTransferProhibited" in record.registration_status
    assert record.created_date == datetime.fromisoformat("2020-01-15T10:00:00+00:00")
    assert record.expiration_date == datetime.fromisoformat("2025-01-15T10:00:00+00:00")
    assert len(record.nameservers) == 2
    assert "ns1.exampledns.com" in record.nameservers
    assert record.is_privacy_redacted is True


def test_m2_live_rdap_lookup_and_persistence(setup_database):
    """Verify live RDAP ingestion for zoniraz.com against Verisign registry."""
    pipeline = RDAPPipeline(cache_days=7)
    result = pipeline.run("zoniraz.com", force=True)

    assert result.status == "completed"
    assert result.is_cached is False
    assert result.registrar_name is not None
    assert "godaddy" in result.registrar_name.lower()
    assert result.created_date is not None
    assert result.expiration_date is not None
    assert result.nameservers_count >= 2

    # Verify database persistence
    with get_db() as session:
        domain = session.query(Domain).filter_by(domain_name="zoniraz.com").first()
        assert domain is not None

        reg = session.query(DomainRegistrationData).filter_by(domain_id=domain.id).first()
        assert reg is not None
        assert reg.registrar_name == result.registrar_name
        reg_created = reg.created_date.replace(tzinfo=result.created_date.tzinfo) if reg.created_date.tzinfo is None else reg.created_date
        assert reg_created == result.created_date
        reg_exp = reg.expiration_date.replace(tzinfo=result.expiration_date.tzinfo) if reg.expiration_date.tzinfo is None else reg.expiration_date
        assert reg_exp == result.expiration_date

        prov = session.get(DataProvenance, reg.provenance_id)
        assert prov is not None
        assert prov.source_id == "rdap"
        assert prov.dataset_identifier == "iana_rdap_rfc7483"


def test_m2_caching_behavior(setup_database):
    """Verify that repeat queries within TTL return cached data without network roundtrip."""
    pipeline = RDAPPipeline(cache_days=7)
    
    # Query zoniraz.com without force - should hit cache
    cached_result = pipeline.run("zoniraz.com", force=False)
    assert cached_result.status == "cached"
    assert cached_result.is_cached is True
    assert cached_result.registrar_name is not None
    assert "godaddy" in cached_result.registrar_name.lower()
