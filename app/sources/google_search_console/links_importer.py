"""
Google Search Console Links Report Importer.
Parses user-exported CSV files from the Search Console 'Links' section,
extracts referring URLs, target pages, and anchor text, and persists records
with strict 'gsc_export' provenance.
"""
import os
import csv
import hashlib
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any
from pydantic import BaseModel
from loguru import logger
from app.database.connection import get_db
from app.database.repositories import CrawlRepository
from app.database.models import CrawlRun, Page, PageLink, LinkOccurrence, DataProvenance


class GSCLinkExportRow(BaseModel):
    source_url: str
    target_url: str
    anchor_text: Optional[str] = None
    link_count: Optional[int] = 1


class GSCImportResult(BaseModel):
    file_path: str
    file_sha256: str
    target_site: str
    crawl_run_id: Optional[int] = None
    rows_parsed: int = 0
    links_ingested: int = 0
    errors_count: int = 0
    status: str


class GSCLinksImporter:
    """Imports Search Console Links report export files (CSV)."""

    def parse_csv_file(self, file_path: str, default_target_site: str) -> List[GSCLinkExportRow]:
        """
        Parse CSV export from Google Search Console Links report.
        Handles various export formats ('Top linking pages', 'Top linking sites', 'Target pages').
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"GSC export file not found: {file_path}")

        rows: List[GSCLinkExportRow] = []
        with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
            # Read first few lines to detect delimiter
            sample = f.read(4096)
            f.seek(0)
            delimiter = ","
            try:
                sniffer = csv.Sniffer()
                dialect = sniffer.sniff(sample)
                delimiter = dialect.delimiter
            except Exception:
                pass

            reader = csv.reader(f, delimiter=delimiter)
            header = None
            for row in reader:
                if not row or not any(row):
                    continue
                # Normalize header
                if header is None:
                    header = [c.strip().lower() for c in row]
                    continue

                row_dict = {header[i]: row[i].strip() for i in range(min(len(header), len(row)))}
                
                # Determine source and target URLs based on header columns
                source_url = None
                target_url = None
                anchor = None

                # Pattern A: 'Incoming links' / 'Top linking pages' (Linking site, Target page)
                if "linking site" in row_dict or "incoming links" in row_dict:
                    source_url = row_dict.get("incoming links") or row_dict.get("linking site")
                    target_url = row_dict.get("target page") or default_target_site
                # Pattern B: 'Top linking sites' (Site, Target pages)
                elif "site" in row_dict:
                    source_url = row_dict.get("site")
                    target_url = default_target_site
                # Pattern C: Generic Source / Target
                elif "source url" in row_dict or "source" in row_dict:
                    source_url = row_dict.get("source url") or row_dict.get("source")
                    target_url = row_dict.get("target url") or row_dict.get("target") or default_target_site
                elif len(row) >= 2 and row[0].startswith("http"):
                    source_url = row[0]
                    target_url = row[1] if row[1].startswith("http") else default_target_site
                elif len(row) >= 1 and row[0].startswith("http"):
                    source_url = row[0]
                    target_url = default_target_site

                anchor = row_dict.get("anchor text") or row_dict.get("anchor") or row_dict.get("text")

                if source_url:
                    rows.append(
                        GSCLinkExportRow(
                            source_url=source_url,
                            target_url=target_url or default_target_site,
                            anchor_text=anchor,
                        )
                    )

        logger.info(f"Parsed {len(rows)} link records from GSC export file {file_path}")
        return rows

    def import_links_report(self, file_path: str, target_site: str) -> GSCImportResult:
        """
        Import GSC Links CSV, calculate file SHA-256, record provenance with 'gsc_export',
        and persist pages, page_links, and link_occurrences.
        """
        clean_target = target_site.strip()
        logger.info(f"Starting GSC Links report import from {file_path} for target site {clean_target}")

        with open(file_path, "rb") as f:
            file_bytes = f.read()
        file_sha256 = hashlib.sha256(file_bytes).hexdigest()
        file_name = os.path.basename(file_path)

        # 1. Initialize crawl run for audit
        with get_db() as session:
            repo = CrawlRepository(session)
            crawl_run = repo.create_crawl_run(
                source_id="gsc_export",
                run_type="gsc_links_file_import",
                target_domain=clean_target,
            )
            crawl_run_id = crawl_run.id

        # 2. Parse CSV
        try:
            link_rows = self.parse_csv_file(file_path, clean_target)
        except Exception as e:
            with get_db() as session:
                repo = CrawlRepository(session)
                repo.record_error(
                    source_id="gsc_export",
                    crawl_run_id=crawl_run_id,
                    stage="csv_parse",
                    error_code="GSC_CSV_PARSE_ERROR",
                    error_message=str(e),
                )
            raise

        # 3. Store provenance and persist entities
        ingested_count = 0
        errors_count = 0

        with get_db() as session:
            repo = CrawlRepository(session)

            # Record file provenance
            prov = repo.record_provenance(
                source_id="gsc_export",
                dataset_identifier=f"gsc_links_export_{file_name}",
                record_locator=file_path,
                downloaded_bytes=len(file_bytes),
                payload_sha256=file_sha256,
                http_status=None,
                content_type="text/csv",
            )

            # Ensure target site page exists
            target_page = repo.get_or_create_page(
                raw_url=clean_target,
                normalized_url=clean_target.rstrip("/").lower(),
            )
            target_page_id = target_page.id
            target_domain_id = target_page.domain_id

            for item in link_rows:
                try:
                    # Ingest referring source page
                    source_page = repo.get_or_create_page(
                        raw_url=item.source_url,
                        normalized_url=item.source_url.rstrip("/").lower(),
                        provenance_id=prov.id,
                    )

                    # Ingest canonical directed link
                    page_link = repo.get_or_create_page_link(
                        source_page_id=source_page.id,
                        target_page_id=target_page_id,
                        source_domain_id=source_page.domain_id,
                        target_domain_id=target_domain_id,
                        provenance_id=prov.id,
                    )

                    # Record link occurrence
                    repo.record_link_occurrence(
                        page_link_id=page_link.id,
                        crawl_run_id=crawl_run_id,
                        source_id="gsc_export",
                        raw_target_url=item.target_url,
                        normalized_target_url=item.target_url.rstrip("/").lower(),
                        anchor_text=item.anchor_text,
                        link_type="a_href",
                        dom_context="gsc_external_link",
                        provenance_id=prov.id,
                    )
                    ingested_count += 1
                except Exception as e:
                    errors_count += 1
                    logger.debug(f"Error persisting GSC row {item}: {e}")

            # Finalize crawl run
            run = session.get(CrawlRun, crawl_run_id)
            if run:
                run.status = "completed" if ingested_count > 0 or len(link_rows) == 0 else "failed"
                run.completed_at = datetime.now(timezone.utc)
                run.records_discovered = len(link_rows)
                run.records_ingested = ingested_count
                run.bytes_downloaded = len(file_bytes)
                run.errors_count = errors_count

        logger.info(f"GSC Links import completed: {ingested_count}/{len(link_rows)} links ingested with provenance 'gsc_export'.")

        return GSCImportResult(
            file_path=file_path,
            file_sha256=file_sha256,
            target_site=clean_target,
            crawl_run_id=crawl_run_id,
            rows_parsed=len(link_rows),
            links_ingested=ingested_count,
            errors_count=errors_count,
            status="completed",
        )
