"""
Unit and Integration Tests for Milestone 3:
Internet Archive Wayback Machine Historical Ingestion Pipeline.
"""
import pytest
from app.database.connection import init_db, get_db
from app.database.models import Domain, Page, HistoricalSnapshot, DataProvenance, CrawlRun, LinkOccurrence
from app.sources.wayback.cdx_client import WaybackCDXClient, WaybackCDXRecord
from app.sources.wayback.snapshot_downloader import WaybackSnapshotDownloader, RawSnapshotResult
from app.sources.wayback.comparator import WaybackComparator, SnapshotComparisonResult
from app.sources.wayback.pipeline import WaybackPipeline


@pytest.fixture(scope="module")
def setup_database():
    init_db()


def test_m3_cdx_query_and_parsing():
    """Verify querying Wayback CDX returns structured snapshot records."""
    client = WaybackCDXClient()
    records = client.query_snapshots("zoniraz.com", limit=2, filter_status_200=True)

    assert len(records) > 0
    for rec in records:
        assert len(rec.timestamp) == 14  # YYYYMMDDhhmmss
        assert "zoniraz.com" in rec.original_url
        assert rec.status_code == 200
        assert len(rec.digest) > 0


def test_m3_raw_snapshot_id_download_no_toolbar():
    """
    Verify raw snapshot downloader uses 'id_' modifier and
    delivers unmodified HTML without Wayback toolbar scripts.
    """
    downloader = WaybackSnapshotDownloader()
    # Verified real snapshot for zoniraz.com
    timestamp = "20201101073521"
    original_url = "https://www.zoniraz.com/"

    res: RawSnapshotResult = downloader.fetch_raw_snapshot(timestamp, original_url)

    assert res.http_status == 200
    assert res.downloaded_bytes > 0
    assert len(res.payload_sha256) == 64
    assert res.is_toolbar_injected is False
    assert "<html" in res.html_payload.lower()
    # Toolbar script markers must NOT be in raw payload
    assert "__wm.init" not in res.html_payload
    assert "wb-toolbar" not in res.html_payload


def test_m3_historical_comparator():
    """Verify historical comparator identifies title changes and outbound link additions/removals."""
    html_v1 = """
    <html>
    <head><title>Old Title</title><meta name="description" content="Old description"></head>
    <body>
        <a href="https://example.com/p1">Page 1</a>
        <a href="https://example.com/p2">Page 2</a>
    </body>
    </html>
    """

    html_v2 = """
    <html>
    <head><title>New Title</title><meta name="description" content="Old description"></head>
    <body>
        <a href="https://example.com/p1">Page 1</a>
        <a href="https://example.com/p3">Page 3</a>
    </body>
    </html>
    """

    comp: SnapshotComparisonResult = WaybackComparator.compare_html_snapshots(
        domain_name="example.com",
        target_url="https://example.com",
        ts_earlier="20200101000000",
        html_earlier=html_v1,
        ts_later="20220101000000",
        html_later=html_v2,
    )

    assert comp.is_content_identical is False
    assert comp.title_changed is True
    assert comp.title_earlier == "Old Title"
    assert comp.title_later == "New Title"
    assert comp.meta_desc_changed is False

    # Link additions / removals
    assert comp.link_diff.total_added == 1
    assert "https://example.com/p3" in comp.link_diff.added_links
    assert comp.link_diff.total_removed == 1
    assert "https://example.com/p2" in comp.link_diff.removed_links
    assert comp.link_diff.total_retained == 1
    assert "https://example.com/p1" in comp.link_diff.retained_links


def test_m3_end_to_end_pipeline_and_persistence(setup_database):
    """Verify Wayback pipeline downloads raw snapshots, extracts links, and persists to DB."""
    pipeline = WaybackPipeline()
    result = pipeline.run("zoniraz.com", limit=1)

    assert result.status in ("completed", "completed_with_errors")
    assert result.snapshots_ingested >= 1
    assert result.total_bytes_downloaded > 0

    with get_db() as session:
        # Check historical snapshot record
        snaps = session.query(HistoricalSnapshot).all()
        assert len(snaps) > 0

        # Check page record
        pages = session.query(Page).all()
        assert len(pages) > 0

        # Check provenance
        prov = session.query(DataProvenance).filter_by(source_id="wayback").first()
        assert prov is not None
        assert prov.dataset_identifier == "wayback_archive"
        assert len(prov.payload_sha256) == 64

        # Check crawl run state
        run = session.get(CrawlRun, result.crawl_run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.records_ingested >= 1
