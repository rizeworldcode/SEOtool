"""
Unit and Integration Tests for Milestone 6:
Data Export (Parquet, CSV, JSON) and System Integrity Audit.
"""
import os
import tempfile
import pytest
import pyarrow.parquet as pq
from app.database.connection import init_db, get_db
from app.database.models import Page, PageLink, LinkOccurrence, DataProvenance
from app.exporters.dataset_exporter import DatasetExporter, SUPPORTED_TABLES


@pytest.fixture(scope="module")
def setup_database():
    init_db()


def test_m6_export_tables_parquet(setup_database):
    """Verify exporting tables to Apache Parquet format."""
    with tempfile.TemporaryDirectory() as temp_dir:
        exporter = DatasetExporter(output_dir=temp_dir)
        
        # Test exporting pages
        res_pages = exporter.export_table("pages", export_format="parquet")
        assert res_pages.export_format == "parquet"
        assert os.path.exists(res_pages.file_path)
        assert res_pages.file_size_bytes > 0
        
        # Verify read by pyarrow
        table = pq.read_table(res_pages.file_path)
        assert table.num_rows == res_pages.row_count
        assert "normalized_url" in table.column_names

        # Test exporting page_links
        res_links = exporter.export_table("page_links", export_format="parquet")
        assert os.path.exists(res_links.file_path)
        table_links = pq.read_table(res_links.file_path)
        assert table_links.num_rows == res_links.row_count


def test_m6_export_tables_csv_and_json(setup_database):
    """Verify exporting tables to CSV and JSON formats."""
    with tempfile.TemporaryDirectory() as temp_dir:
        exporter = DatasetExporter(output_dir=temp_dir)

        # CSV export
        res_csv = exporter.export_table("link_occurrences", export_format="csv")
        assert res_csv.export_format == "csv"
        assert os.path.exists(res_csv.file_path)
        with open(res_csv.file_path, "r", encoding="utf-8") as f:
            header = f.readline()
            assert "raw_target_url" in header or "id" in header

        # JSON export
        res_json = exporter.export_table("domain_registration_data", export_format="json")
        assert res_json.export_format == "json"
        assert os.path.exists(res_json.file_path)


def test_m6_all_supported_tables(setup_database):
    """Verify that every supported table can be extracted and exported without errors."""
    with tempfile.TemporaryDirectory() as temp_dir:
        exporter = DatasetExporter(output_dir=temp_dir)
        for tbl in SUPPORTED_TABLES:
            res = exporter.export_table(tbl, export_format="parquet")
            assert os.path.exists(res.file_path)
            assert res.table_name == tbl


def test_m6_system_provenance_audit(setup_database):
    """Verify system-wide data integrity and provenance completeness."""
    with get_db() as session:
        pages_total = session.query(Page).count()
        prov_total = session.query(DataProvenance).count()
        occs_total = session.query(LinkOccurrence).count()
        occs_with_prov = session.query(LinkOccurrence).filter(LinkOccurrence.provenance_id.isnot(None)).count()

        # Database must have active data
        assert pages_total > 0
        assert prov_total > 0
        assert occs_total > 0

        # 100% of physical link occurrences must have strict provenance
        occ_coverage = (occs_with_prov / occs_total) * 100.0
        assert occ_coverage == 100.0

        # Provenance must be actively attached to ingested crawled entities
        pages_with_prov = session.query(Page).filter(Page.provenance_id.isnot(None)).count()
        assert pages_with_prov > 0
        assert prov_total > 0
