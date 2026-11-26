"""
Unit and Integration Tests for Milestone 5:
Google Search Console Dual Ingestion Pipeline (API & Links File Import).
"""
import os
import tempfile
import pytest
from app.database.connection import init_db, get_db
from app.database.models import Page, PageLink, LinkOccurrence, DataProvenance, CrawlRun, SourceSyncHistory
from app.sources.google_search_console.links_importer import GSCLinksImporter, GSCImportResult
from app.sources.google_search_console.api_client import (
    GSCAPIClient,
    GSCSearchAnalyticsResult,
    GSCSearchAnalyticsRow,
)
from app.sources.google_search_console.pipeline import GSCPipeline, GSCAnalyticsSyncResult


@pytest.fixture(scope="module")
def setup_database():
    init_db()


def test_m5_gsc_links_csv_import(setup_database):
    """
    Verify GSC Links report CSV file import:
    1. Parse source URLs, target URLs, and anchor text.
    2. Persist page links and link occurrences.
    3. Record data provenance with source_id='gsc_export' and file SHA-256.
    """
    csv_content = """Incoming links,Target page,Anchor text
https://fashion-blog-xyz.com/best-rings-2023,https://zoniraz.com/rings,Designer Rings
https://bridal-magazine.org/wedding-jewellery,https://zoniraz.com/,Online Jewellery India
https://gemstone-guide.net/solitaire-reviews,https://zoniraz.com/solitaires,Solitaire Buying Guide
"""
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(csv_content)
        temp_csv_path = f.name

    try:
        importer = GSCLinksImporter()
        result: GSCImportResult = importer.import_links_report(
            file_path=temp_csv_path,
            target_site="https://zoniraz.com",
        )

        assert result.status == "completed"
        assert result.rows_parsed == 3
        assert result.links_ingested == 3
        assert len(result.file_sha256) == 64

        with get_db() as session:
            # Verify provenance
            prov = session.query(DataProvenance).filter_by(source_id="gsc_export").first()
            assert prov is not None
            assert prov.payload_sha256 == result.file_sha256
            assert prov.content_type == "text/csv"

            # Verify link occurrences
            occs = session.query(LinkOccurrence).filter_by(source_id="gsc_export").all()
            assert len(occs) >= 3
            anchors = [o.anchor_text for o in occs]
            assert "Designer Rings" in anchors
            assert "Online Jewellery India" in anchors

            # Verify crawl run state
            run = session.get(CrawlRun, result.crawl_run_id)
            assert run is not None
            assert run.status == "completed"
            assert run.records_ingested == 3
    finally:
        if os.path.exists(temp_csv_path):
            os.remove(temp_csv_path)


def test_m5_gsc_api_search_analytics_schema():
    """Verify Search Analytics API schema deserialization and metrics types."""
    raw_response = {
        "rows": [
            {
                "keys": ["jewellery store online", "https://zoniraz.com/"],
                "clicks": 150.0,
                "impressions": 3200.0,
                "ctr": 0.046875,
                "position": 3.7,
            }
        ],
        "responseAggregationType": "byPage",
    }

    client = GSCAPIClient()
    rows = [
        GSCSearchAnalyticsRow(
            keys=r["keys"],
            clicks=float(r["clicks"]),
            impressions=float(r["impressions"]),
            ctr=float(r["ctr"]),
            position=float(r["position"]),
        )
        for r in raw_response["rows"]
    ]
    res = GSCSearchAnalyticsResult(
        site_url="https://zoniraz.com",
        start_date="2026-10-01",
        end_date="2026-10-31",
        dimensions=["query", "page"],
        rows=rows,
        response_aggregation_type=raw_response["responseAggregationType"],
    )

    assert len(res.rows) == 1
    assert res.rows[0].clicks == 150.0
    assert res.rows[0].position == 3.7
    assert res.rows[0].keys[0] == "jewellery store online"


def test_m5_gsc_bigquery_distinction():
    """
    Verify architectural validation:
    Google Search Console BigQuery bulk export exports performance tables only,
    and strictly does NOT automatically export the Links report.
    """
    info = GSCAPIClient.get_bigquery_export_dataset_schema()
    assert info["links_report_in_bigquery"] is False
    for table in info["tables"]:
        assert table["contains_backlinks"] is False


def test_m5_gsc_pipeline_analytics_mock_sync(setup_database):
    """Verify Search Analytics pipeline execution, crawl run audit, and sync history."""
    pipeline = GSCPipeline()
    result: GSCAnalyticsSyncResult = pipeline.sync_analytics(
        site_url="https://zoniraz.com",
        start_date="2026-10-01",
        end_date="2026-10-31",
        mock=True,
    )

    assert result.status == "completed"
    assert result.is_mock is True
    assert result.rows_synced >= 2

    with get_db() as session:
        sync_hist = (
            session.query(SourceSyncHistory)
            .filter_by(source_id="gsc_api", crawl_run_id=result.crawl_run_id)
            .first()
        )
        assert sync_hist is not None
        assert sync_hist.status == "success_mock"
        assert sync_hist.items_count >= 2

        run = session.get(CrawlRun, result.crawl_run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.records_ingested == result.rows_synced
