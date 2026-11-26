"""
Dataset Exporter Module for LinkPilot Data Engine.
Exports normalized database tables to Apache Parquet, CSV, and JSON format
for downstream data analysis, warehousing, and authority computation.
"""
import os
import json
import csv
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import pyarrow as pa
import pyarrow.parquet as pq
from loguru import logger
from app.config.settings import settings
from app.database.connection import get_db
from app.database.models import (
    Page,
    PageLink,
    LinkOccurrence,
    DomainRegistrationData,
    HistoricalSnapshot,
    DataProvenance,
    CrawlRun,
    Domain,
)

SUPPORTED_TABLES = [
    "pages",
    "page_links",
    "link_occurrences",
    "domain_registration_data",
    "historical_snapshots",
    "data_provenance",
    "crawl_runs",
]


class ExportResult:
    def __init__(self, table_name: str, export_format: str, file_path: str, row_count: int, file_size_bytes: int):
        self.table_name = table_name
        self.export_format = export_format
        self.file_path = file_path
        self.row_count = row_count
        self.file_size_bytes = file_size_bytes


class DatasetExporter:
    """Exports LinkPilot database tables into Parquet, CSV, or JSON datasets."""

    def __init__(self, output_dir: Optional[str] = None):
        self.output_dir = output_dir or str(settings.EXPORTS_DIR)
        os.makedirs(self.output_dir, exist_ok=True)

    def extract_table_rows(self, table_name: str) -> List[Dict[str, Any]]:
        """Fetch all rows from the specified table as dictionaries."""
        with get_db() as session:
            rows: List[Dict[str, Any]] = []
            if table_name == "pages":
                records = session.query(Page).all()
                for r in records:
                    rows.append({
                        "id": r.id,
                        "domain_id": r.domain_id,
                        "raw_url": r.raw_url,
                        "normalized_url": r.normalized_url,
                        "canonical_url": r.canonical_url,
                        "http_status": r.http_status,
                        "mime_type": r.mime_type,
                        "truncation_reason": r.truncation_reason,
                        "first_discovered_at": str(r.first_discovered_at) if r.first_discovered_at else None,
                        "last_crawled_at": str(r.last_crawled_at) if r.last_crawled_at else None,
                        "provenance_id": r.provenance_id,
                    })
            elif table_name == "page_links":
                records = session.query(PageLink).all()
                for r in records:
                    rows.append({
                        "id": r.id,
                        "source_page_id": r.source_page_id,
                        "target_page_id": r.target_page_id,
                        "source_domain_id": r.source_domain_id,
                        "target_domain_id": r.target_domain_id,
                        "first_seen_at": str(r.first_seen_at) if r.first_seen_at else None,
                        "last_seen_at": str(r.last_seen_at) if r.last_seen_at else None,
                        "is_active": r.is_active,
                        "provenance_id": r.provenance_id,
                    })
            elif table_name == "link_occurrences":
                records = session.query(LinkOccurrence).all()
                for r in records:
                    rows.append({
                        "id": r.id,
                        "page_link_id": r.page_link_id,
                        "crawl_run_id": r.crawl_run_id,
                        "source_id": r.source_id,
                        "raw_target_url": r.raw_target_url,
                        "normalized_target_url": r.normalized_target_url,
                        "anchor_text": r.anchor_text,
                        "rel_attributes": r.rel_attributes,
                        "link_type": r.link_type,
                        "link_position": r.link_position,
                        "dom_context": r.dom_context,
                        "provenance_id": r.provenance_id,
                        "observed_at": str(r.observed_at) if r.observed_at else None,
                    })
            elif table_name == "domain_registration_data":
                records = session.query(DomainRegistrationData).all()
                for r in records:
                    rows.append({
                        "id": r.id,
                        "domain_id": r.domain_id,
                        "registry_domain_id": r.registry_domain_id,
                        "registrar_name": r.registrar_name,
                        "registrar_iana_id": r.registrar_iana_id,
                        "registration_status": r.registration_status,
                        "created_date": str(r.created_date) if r.created_date else None,
                        "updated_date": str(r.updated_date) if r.updated_date else None,
                        "expiration_date": str(r.expiration_date) if r.expiration_date else None,
                        "nameservers": r.nameservers,
                        "registrant_organization": r.registrant_organization,
                        "registrant_country": r.registrant_country,
                        "is_privacy_redacted": r.is_privacy_redacted,
                        "last_checked_at": str(r.last_checked_at) if r.last_checked_at else None,
                        "provenance_id": r.provenance_id,
                    })
            elif table_name == "historical_snapshots":
                records = session.query(HistoricalSnapshot).all()
                for r in records:
                    rows.append({
                        "id": r.id,
                        "domain_id": r.domain_id,
                        "page_id": r.page_id,
                        "crawl_run_id": r.crawl_run_id,
                        "timestamp": r.timestamp,
                        "snapshot_url": r.snapshot_url,
                        "original_url": r.original_url,
                        "http_status": r.http_status,
                        "mime_type": r.mime_type,
                        "digest": r.digest,
                        "content_sha256": r.content_sha256,
                        "downloaded_bytes": r.downloaded_bytes,
                        "title": r.title,
                        "meta_description": r.meta_description,
                        "outbound_links_count": r.outbound_links_count,
                        "captured_at": str(r.captured_at) if r.captured_at else None,
                        "ingested_at": str(r.ingested_at) if r.ingested_at else None,
                        "provenance_id": r.provenance_id,
                    })
            elif table_name == "data_provenance":
                records = session.query(DataProvenance).all()
                for r in records:
                    rows.append({
                        "id": r.id,
                        "source_id": r.source_id,
                        "dataset_identifier": r.dataset_identifier,
                        "record_locator": r.record_locator,
                        "warc_record_id": r.warc_record_id,
                        "warc_type": r.warc_type,
                        "http_status": r.http_status,
                        "content_type": r.content_type,
                        "warc_truncated": r.warc_truncated,
                        "downloaded_bytes": r.downloaded_bytes,
                        "record_compressed_sha256": r.record_compressed_sha256,
                        "record_decompressed_sha256": r.record_decompressed_sha256,
                        "payload_sha256": r.payload_sha256,
                        "warc_payload_digest": r.warc_payload_digest,
                        "retrieval_timestamp": str(r.retrieval_timestamp) if r.retrieval_timestamp else None,
                    })
            elif table_name == "crawl_runs":
                records = session.query(CrawlRun).all()
                for r in records:
                    rows.append({
                        "id": r.id,
                        "source_id": r.source_id,
                        "run_type": r.run_type,
                        "status": r.status,
                        "target_domain": r.target_domain,
                        "records_discovered": r.records_discovered,
                        "records_ingested": r.records_ingested,
                        "bytes_downloaded": r.bytes_downloaded,
                        "errors_count": r.errors_count,
                        "started_at": str(r.started_at) if r.started_at else None,
                        "completed_at": str(r.completed_at) if r.completed_at else None,
                    })
            else:
                raise ValueError(f"Unsupported table for export: {table_name}. Supported: {SUPPORTED_TABLES}")

            return rows

    def export_table(self, table_name: str, export_format: str = "parquet") -> ExportResult:
        """Export specified table into parquet, csv, or json format."""
        clean_table = table_name.strip().lower()
        fmt = export_format.strip().lower()
        if fmt not in ("parquet", "csv", "json"):
            raise ValueError(f"Invalid export format: {fmt}. Supported formats: 'parquet', 'csv', 'json'.")

        rows = self.extract_table_rows(clean_table)
        ts_slug = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{clean_table}_{ts_slug}.{fmt}"
        file_path = os.path.join(self.output_dir, filename)

        if fmt == "parquet":
            if rows:
                table = pa.Table.from_pylist(rows)
            else:
                table = pa.Table.from_pylist([{"dummy": None}])
            pq.write_table(table, file_path, compression="snappy")
        elif fmt == "csv":
            if rows:
                fieldnames = list(rows[0].keys())
                with open(file_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
            else:
                with open(file_path, "w", newline="", encoding="utf-8") as f:
                    f.write("id\n")
        elif fmt == "json":
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(rows, f, indent=2)

        file_size = os.path.getsize(file_path)
        logger.info(f"Exported {len(rows)} rows from table '{clean_table}' to {file_path} ({file_size:,} bytes)")

        return ExportResult(
            table_name=clean_table,
            export_format=fmt,
            file_path=file_path,
            row_count=len(rows),
            file_size_bytes=file_size,
        )
