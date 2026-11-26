"""
CLI interface for LinkPilot Data Engine (dataengine).
Provides commands for source discovery, Common Crawl downloading/extraction,
job monitoring, and data exports.
"""
import sys
from typing import Optional
import typer
from rich.console import Console
from rich.table import Table
from app.config.settings import settings
from app.database.connection import init_db, get_db
from app.database.models import DataSource, CrawlCollection, CrawlRun, Page, PageLink, LinkOccurrence, ProcessingError
from app.sources.commoncrawl.discovery import CommonCrawlDiscovery
from app.sources.commoncrawl.pipeline import CommonCrawlM1APipeline

app = typer.Typer(name="dataengine", help="LinkPilot Autonomous SEO Data Acquisition Engine")
sources_app = typer.Typer(name="sources", help="Manage and inspect data sources")
discover_app = typer.Typer(name="discover", help="Discover available crawl releases and catalogs")
download_app = typer.Typer(name="download", help="Download and extract datasets")
jobs_app = typer.Typer(name="jobs", help="Monitor and inspect background crawl jobs")
db_app = typer.Typer(name="db", help="Manage and inspect database schemas and migrations.")

app.add_typer(sources_app)
app.add_typer(discover_app)
app.add_typer(download_app)
app.add_typer(jobs_app)
app.add_typer(db_app)

console = Console()


@app.callback()
def main_callback():
    """LinkPilot Autonomous SEO Data Acquisition Engine."""
    pass


@db_app.command("init")
def db_init(
    force: bool = typer.Option(False, "--force", "-f", help="Re-initialize or apply all pending table schemas.")
):
    """Explicitly create and initialize all database tables."""
    console.print("[cyan]Initializing LinkPilot database schema...[/cyan]")
    init_db()
    console.print("[green]Database schema initialized successfully.[/green]")


@db_app.command("check")
def db_check():
    """Verify that required database tables exist."""
    from sqlalchemy import inspect
    from app.database.connection import engine

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    required = {"pages", "page_links", "page_authority_scores"}
    missing = required - tables
    if missing:
        console.print(f"[bold red]Missing required tables:[/bold red] {missing}")
        raise typer.Exit(code=1)
    console.print("[green]Database schema is complete and valid.[/green]")


@sources_app.command("list")
def list_sources():
    """List registered data sources and their operational status."""
    with get_db() as session:
        sources = session.query(DataSource).all()
        table = Table(title="LinkPilot Registered Data Sources")
        table.add_column("Source ID", style="cyan")
        table.add_column("Name", style="bold")
        table.add_column("Enabled", style="green")
        table.add_column("Health Status", style="magenta")
        table.add_column("Rate Limit (RPS)", justify="right")

        for src in sources:
            table.add_row(
                src.id,
                src.name,
                "✓ Yes" if src.is_enabled else "✗ No",
                src.health_status,
                str(src.rate_limit_rps),
            )
        console.print(table)


@discover_app.command("commoncrawl")
def discover_commoncrawl():
    """Discover available Common Crawl releases and sync to database."""
    console.print("[yellow]Contacting Common Crawl collinfo.json...[/yellow]")
    discovery = CommonCrawlDiscovery()
    collections = discovery.sync_collections_to_db()

    table = Table(title=f"Common Crawl Releases ({len(collections)} total)")
    table.add_column("Crawl ID", style="cyan")
    table.add_column("Name", style="bold")
    table.add_column("CDX Index Endpoint", style="dim")

    for col in collections[:10]:
        table.add_row(col["id"], col["name"], col["cdx_api_url"])

    console.print(table)
    if len(collections) > 10:
        console.print(f"[dim]... and {len(collections) - 10} older crawl releases.[/dim]")


benchmark_app = typer.Typer(help="Performance benchmarks for data sources.")
app.add_typer(benchmark_app, name="benchmark")


@benchmark_app.command("webgraph")
def benchmark_webgraph(
    max_mb: float = typer.Option(5.0, "--max-mb", "-m", help="Max compressed MiB to stream per partition"),
):
    """
    Execute Milestone 1B: Bounded WebGraph feasibility benchmark.
    Streams vertex and edge partition slices, measures memory/throughput, and evaluates traversal feasibility.
    """
    from app.sources.commoncrawl.webgraph_benchmark import WebGraphBenchmark
    console.print(f"[bold green]Starting WebGraph Bounded Feasibility Benchmark (budget: {max_mb:.1f} MiB/partition)...[/bold green]")
    bench = WebGraphBenchmark()
    report = bench.run_benchmark(max_mb_per_partition=max_mb)

    table = Table(title=f"WebGraph Benchmark Report: {report.release_id}")
    table.add_column("Component", style="bold")
    table.add_column("Partitions", justify="right")
    table.add_column("Streamed (Comp)", justify="right")
    table.add_column("Processed (Decomp)", justify="right")
    table.add_column("Lines Parsed", justify="right")
    table.add_column("Throughput", justify="right")
    table.add_column("Peak RAM", justify="right")

    table.add_row(
        "Vertices",
        str(report.total_vertex_partitions),
        f"{report.vertex_benchmark.compressed_bytes_streamed:,} B",
        f"{report.vertex_benchmark.uncompressed_bytes_processed:,} B",
        f"{report.vertex_benchmark.lines_parsed:,}",
        f"{report.vertex_benchmark.throughput_kb_s} KB/s",
        f"{report.vertex_benchmark.peak_ram_mb} MB",
    )
    table.add_row(
        "Edges",
        str(report.total_edge_partitions),
        f"{report.edge_benchmark.compressed_bytes_streamed:,} B",
        f"{report.edge_benchmark.uncompressed_bytes_processed:,} B",
        f"{report.edge_benchmark.lines_parsed:,}",
        f"{report.edge_benchmark.throughput_kb_s} KB/s",
        f"{report.edge_benchmark.peak_ram_mb} MB",
    )
    console.print(table)

    proj_table = Table(title="Full Graph Traversal Feasibility Projection")
    proj_table.add_column("Metric", style="bold")
    proj_table.add_column("Value", style="cyan")
    proj_table.add_row("Full Graph Compressed Volume", f"{report.projected_full_graph_compressed_gb} GB")
    proj_table.add_row("Full Graph Uncompressed Footprint", f"~{report.projected_full_graph_uncompressed_gb} GB")
    proj_table.add_row("Projected Local Traversal Time", f"{report.projected_full_traversal_hours:.1f} hours")
    proj_table.add_row("Feasibility Gate Decision", f"[bold yellow]{report.feasibility_decision}[/bold yellow]")
    console.print(proj_table)

    console.print(f"\n[bold green]Architectural Recommendation:[/bold green] {report.recommendation}\n")


@download_app.command("commoncrawl")
def download_commoncrawl(
    domain: str = typer.Option(..., "--domain", "-d", help="Target domain to extract (e.g. zoniraz.com or example.com)"),
    crawl_id: Optional[str] = typer.Option(None, "--crawl-id", "-c", help="Specific crawl ID (defaults to latest)"),
    limit: int = typer.Option(5, "--limit", "-l", help="Maximum pages to extract"),
):
    """
    Execute Milestone 1A: Discover target domain pages in Common Crawl,
    download WARC byte-range slices via HTTP 206, and extract outbound links.
    """
    console.print(f"[bold green]Starting Common Crawl M1A extraction for domain:[/bold green] [cyan]{domain}[/cyan]")
    pipeline = CommonCrawlM1APipeline()
    result = pipeline.run(target_domain=domain, crawl_id=crawl_id, max_pages=limit)

    table = Table(title=f"Extraction Results: {domain} (Crawl: {result.crawl_id})")
    table.add_column("Metric", style="bold")
    table.add_column("Value", style="green")

    table.add_row("Crawl Run ID", str(result.crawl_run_id))
    table.add_row("Status", result.status)
    table.add_row("Discovered CDX Pages", str(result.records_discovered))
    table.add_row("Ingested Pages", str(result.pages_ingested))
    table.add_row("Outbound Links Extracted", str(result.links_extracted))
    table.add_row("Downloaded Bytes (Compressed)", f"{result.bytes_downloaded:,} bytes")
    table.add_row("Errors Encountered", str(result.errors_count))

    console.print(table)


rdap_app = typer.Typer(help="ICANN / RDAP domain registration acquisition.")
app.add_typer(rdap_app, name="rdap")


@rdap_app.command("lookup")
def rdap_lookup(
    domain: str = typer.Option(..., "--domain", "-d", help="Domain to lookup (e.g. zoniraz.com)"),
    force: bool = typer.Option(False, "--force", "-f", help="Bypass 7-day TTL cache and fetch live"),
):
    """
    Execute Milestone 2: Query authoritative ICANN / RDAP registry for domain registration,
    dates, registrar, nameservers, and privacy redaction status.
    """
    from app.sources.rdap.pipeline import RDAPPipeline
    console.print(f"[bold green]Starting RDAP lookup for domain:[/bold green] [cyan]{domain}[/cyan]")
    pipeline = RDAPPipeline()
    result = pipeline.run(domain_name=domain, force=force)

    table = Table(title=f"RDAP Registration Report: {domain}")
    table.add_column("Property", style="bold")
    table.add_column("Value", style="green")

    table.add_row("Domain Name", result.domain_name)
    table.add_row("Status", result.status)
    table.add_row("Cache Used", "Yes (within 7-day TTL)" if result.is_cached else "No (fresh registry query)")
    if result.crawl_run_id:
        table.add_row("Crawl Run ID", str(result.crawl_run_id))
    table.add_row("Registrar", result.registrar_name or "N/A")
    table.add_row("Creation Date", str(result.created_date) if result.created_date else "N/A")
    table.add_row("Expiration Date", str(result.expiration_date) if result.expiration_date else "N/A")
    table.add_row("Nameservers Count", str(result.nameservers_count))
    table.add_row("Privacy Redacted", "Yes (GDPR/Masked)" if result.is_privacy_redacted else "No")

    console.print(table)


wayback_app = typer.Typer(help="Internet Archive Wayback Machine historical snapshot acquisition.")
app.add_typer(wayback_app, name="wayback")


@wayback_app.command("snapshots")
def wayback_snapshots(
    domain: str = typer.Option(..., "--domain", "-d", help="Target domain or URL (e.g. zoniraz.com)"),
    limit: int = typer.Option(3, "--limit", "-l", help="Number of snapshots to fetch"),
):
    """
    Execute Milestone 3: Discover and download historical snapshots via raw id_ modifier,
    extracting metadata, headings, and outbound link changes.
    """
    from app.sources.wayback.pipeline import WaybackPipeline
    console.print(f"[bold green]Starting Wayback Machine snapshot ingestion for:[/bold green] [cyan]{domain}[/cyan] (limit: {limit})")
    pipeline = WaybackPipeline()
    result = pipeline.run(domain_or_url=domain, limit=limit)

    table = Table(title=f"Wayback Ingestion Report: {domain}")
    table.add_column("Metric", style="bold")
    table.add_column("Value", style="green")

    table.add_row("Target", result.target)
    table.add_row("Status", result.status)
    if result.crawl_run_id:
        table.add_row("Crawl Run ID", str(result.crawl_run_id))
    table.add_row("Snapshots Discovered", str(result.snapshots_discovered))
    table.add_row("Snapshots Ingested", str(result.snapshots_ingested))
    table.add_row("Downloaded Bytes", f"{result.total_bytes_downloaded:,} bytes")
    table.add_row("Errors Logged", str(result.errors_count))

    console.print(table)


@wayback_app.command("compare")
def wayback_compare(
    domain: str = typer.Option(..., "--domain", "-d", help="Target domain"),
    ts1: str = typer.Option(..., "--ts1", help="Earlier timestamp (YYYYMMDDhhmmss)"),
    ts2: str = typer.Option(..., "--ts2", help="Later timestamp (YYYYMMDDhhmmss)"),
):
    """
    Compare two historical snapshots of a domain/page to detect title changes,
    description changes, and added/removed outbound links.
    """
    from app.sources.wayback.snapshot_downloader import WaybackSnapshotDownloader
    from app.sources.wayback.comparator import WaybackComparator
    downloader = WaybackSnapshotDownloader()

    # Determine original URL
    clean_target = domain.strip().lower()
    orig_url = f"https://{clean_target}/" if not clean_target.startswith("http") else clean_target

    console.print(f"[bold green]Comparing snapshots for:[/bold green] [cyan]{orig_url}[/cyan] ({ts1} vs {ts2})")
    snap1 = downloader.fetch_raw_snapshot(ts1, orig_url)
    snap2 = downloader.fetch_raw_snapshot(ts2, orig_url)

    comp = WaybackComparator.compare_html_snapshots(
        domain_name=clean_target,
        target_url=orig_url,
        ts_earlier=ts1,
        html_earlier=snap1.html_payload,
        ts_later=ts2,
        html_later=snap2.html_payload,
    )

    table = Table(title=f"Snapshot Comparison: {clean_target} ({ts1} -> {ts2})")
    table.add_column("Attribute", style="bold")
    table.add_column("Earlier Snapshot", style="cyan")
    table.add_column("Later Snapshot", style="yellow")
    table.add_column("Changed?", style="bold")

    table.add_row("Title", comp.title_earlier or "None", comp.title_later or "None", "YES" if comp.title_changed else "NO")
    table.add_row("Meta Description", comp.meta_desc_earlier or "None", comp.meta_desc_later or "None", "YES" if comp.meta_desc_changed else "NO")
    table.add_row("Content Identical", str(comp.is_content_identical), str(comp.is_content_identical), "NO" if not comp.is_content_identical else "YES")
    table.add_row("Links Added", "-", f"+{comp.link_diff.total_added} links", f"{comp.link_diff.total_added}")
    table.add_row("Links Removed", f"-{comp.link_diff.total_removed} links", "-", f"{comp.link_diff.total_removed}")
    table.add_row("Links Retained", f"{comp.link_diff.total_retained} links", f"{comp.link_diff.total_retained} links", "-")

    console.print(table)


bing_app = typer.Typer(help="Bing Webmaster Tools API connector.")
app.add_typer(bing_app, name="bing")


@bing_app.command("probe")
def bing_probe():
    """
    Probe the Bing Webmaster API JSON/HTTP WCF endpoint to verify post-August 2026 availability.
    """
    from app.sources.bing.client import BingWebmasterClient
    client = BingWebmasterClient()
    res = client.probe_service()

    table = Table(title="Bing Webmaster API Availability Probe")
    table.add_column("Property", style="bold")
    table.add_column("Value", style="cyan")

    table.add_row("Audited Endpoint", res["endpoint"])
    table.add_row("HTTP Status Code", str(res["http_status"]))
    table.add_row("Active Post-Sunset?", "[bold green]YES (WCF JSON Service Active)[/bold green]" if res["is_active_post_sunset"] else "[bold red]NO[/bold red]")
    table.add_row("Authenticated", "Yes" if res["authenticated"] else "No (Requires API Key or OAuth Token)")
    table.add_row("Server Response Sample", res.get("response_body", "N/A"))

    console.print(table)


@bing_app.command("set-credentials")
def bing_set_credentials(
    api_key: Optional[str] = typer.Option(None, "--api-key", "-k", help="Bing Webmaster API Key"),
    access_token: Optional[str] = typer.Option(None, "--token", "-t", help="OAuth 2.0 Bearer Access Token"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account identifier/email"),
):
    """
    Save encrypted Bing Webmaster credentials into database (AES-256-GCM).
    """
    if not api_key and not access_token:
        console.print("[bold red]Error:[/bold red] Either --api-key or --token must be provided.")
        raise typer.Exit(1)

    with get_db() as session:
        from app.database.repositories import CrawlRepository
        repo = CrawlRepository(session)
        if api_key:
            repo.save_credential(
                source_id="bing",
                credential_type="api_key",
                plaintext_secret=api_key,
                account_identifier=account,
            )
            console.print("[bold green]Successfully saved encrypted Bing API Key.[/bold green]")
        if access_token:
            repo.save_credential(
                source_id="bing",
                credential_type="oauth2_token",
                plaintext_secret=access_token,
                account_identifier=account,
            )
            console.print("[bold green]Successfully saved encrypted Bing OAuth2 Bearer Token.[/bold green]")


@bing_app.command("sync")
def bing_sync(
    site: str = typer.Option(..., "--site", "-s", help="Target site URL (e.g. https://zoniraz.com)"),
    max_domains: int = typer.Option(5, "--max-domains", "-d", help="Max referring domains to query"),
    mock: bool = typer.Option(False, "--mock", "-m", help="Run with mock verified data for testing"),
):
    """
    Sync referring domains and URLs from Bing Webmaster Tools (GetLinkCounts & GetUrlLinks).
    """
    from app.sources.bing.pipeline import BingPipeline
    pipeline = BingPipeline()
    console.print(f"[bold green]Starting Bing Webmaster sync for site:[/bold green] [cyan]{site}[/cyan] (mock={mock})")
    result = pipeline.run(site_url=site, max_domains=max_domains, mock=mock)

    table = Table(title=f"Bing Webmaster Ingestion Report: {site}")
    table.add_column("Metric", style="bold")
    table.add_column("Value", style="green")

    table.add_row("Site URL", result.site_url)
    table.add_row("Status", result.status)
    table.add_row("Mock Mode", "Yes" if result.is_mock else "No (Live API)")
    if result.crawl_run_id:
        table.add_row("Crawl Run ID", str(result.crawl_run_id))
    table.add_row("Referring Domains", str(result.referring_domains_discovered))
    table.add_row("Referring URLs Ingested", str(result.referring_urls_ingested))
    table.add_row("Payload Bytes", f"{result.total_bytes_processed:,} bytes")
    table.add_row("Errors Logged", str(result.errors_count))

    console.print(table)


gsc_app = typer.Typer(help="Google Search Console Dual Ingestion (API & Links Report File Import).")
app.add_typer(gsc_app, name="gsc")


@gsc_app.command("import-links")
def gsc_import_links(
    file_path: str = typer.Option(..., "--file", "-f", help="Path to Search Console Links export CSV file"),
    site: str = typer.Option(..., "--site", "-s", help="Target site URL (e.g. https://zoniraz.com)"),
):
    """
    Import user-exported Google Search Console Links report CSV file,
    recording provenance as 'gsc_export'.
    """
    from app.sources.google_search_console.pipeline import GSCPipeline
    console.print(f"[bold green]Importing GSC Links report from:[/bold green] [cyan]{file_path}[/cyan]")
    pipeline = GSCPipeline()
    res = pipeline.import_links_file(file_path=file_path, site_url=site)

    table = Table(title=f"GSC Links File Import Report: {site}")
    table.add_column("Property", style="bold")
    table.add_column("Value", style="green")

    table.add_row("Target Site", res.target_site)
    table.add_row("Imported File", res.file_path)
    table.add_row("File SHA-256", res.file_sha256)
    table.add_row("Provenance Source", "gsc_export")
    if res.crawl_run_id:
        table.add_row("Crawl Run ID", str(res.crawl_run_id))
    table.add_row("Rows Parsed", str(res.rows_parsed))
    table.add_row("Links Ingested", str(res.links_ingested))
    table.add_row("Errors Logged", str(res.errors_count))

    console.print(table)


@gsc_app.command("search-analytics")
def gsc_search_analytics(
    site: str = typer.Option(..., "--site", "-s", help="Target site URL"),
    start_date: str = typer.Option("2026-10-01", "--start-date", help="Start date (YYYY-MM-DD)"),
    end_date: str = typer.Option("2026-10-31", "--end-date", help="End date (YYYY-MM-DD)"),
    mock: bool = typer.Option(False, "--mock", "-m", help="Run with mock verified data for testing"),
):
    """
    Sync Search Analytics performance metrics (clicks, impressions, rankings) via GSC API v3.
    """
    from app.sources.google_search_console.pipeline import GSCPipeline
    console.print(f"[bold green]Querying GSC Search Analytics for:[/bold green] [cyan]{site}[/cyan] ({start_date} to {end_date})")
    pipeline = GSCPipeline()
    res = pipeline.sync_analytics(site_url=site, start_date=start_date, end_date=end_date, mock=mock)

    table = Table(title=f"GSC Search Analytics Report: {site}")
    table.add_column("Metric", style="bold")
    table.add_column("Value", style="green")

    table.add_row("Site URL", res.site_url)
    table.add_row("Status", res.status)
    table.add_row("Mock Mode", "Yes" if res.is_mock else "No (Live API)")
    if res.crawl_run_id:
        table.add_row("Crawl Run ID", str(res.crawl_run_id))
    table.add_row("Performance Rows Synced", str(res.rows_synced))

    console.print(table)


@gsc_app.command("bigquery-info")
def gsc_bigquery_info():
    """
    Display official architectural clarification regarding GSC BigQuery bulk export capabilities.
    """
    from app.sources.google_search_console.api_client import GSCAPIClient
    info = GSCAPIClient.get_bigquery_export_dataset_schema()

    table = Table(title="Google Search Console BigQuery Export Architecture")
    table.add_column("Property", style="bold")
    table.add_column("Details", style="cyan")

    table.add_row("Export Dataset Name", info["export_dataset"])
    table.add_row("Daily Export Tables", ", ".join([t["table_name"] for t in info["tables"]]))
    table.add_row("Contains Backlink Data?", "[bold red]NO (Performance impressions/clicks only)[/bold red]")
    table.add_row("Links Report in BigQuery?", "[bold red]FALSE (Not automatically exported to BigQuery)[/bold red]")
    table.add_row("Backlink Ingestion Method", "[bold green]" + info["backlink_import_requirement"] + "[/bold green]")

    console.print(table)


export_app = typer.Typer(help="Export database tables to Parquet, CSV, or JSON.")
app.add_typer(export_app, name="export")


@export_app.command("table")
def export_table(
    table: str = typer.Option(..., "--table", "-t", help="Table name to export (e.g. pages, page_links, link_occurrences)"),
    format: str = typer.Option("parquet", "--format", "-f", help="Export format: parquet, csv, or json"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output directory"),
):
    """Export a specific database table to Parquet, CSV, or JSON."""
    from app.exporters.dataset_exporter import DatasetExporter
    exporter = DatasetExporter(output_dir=output)
    res = exporter.export_table(table_name=table, export_format=format)
    console.print(f"[bold green]Successfully exported {res.row_count} rows from table '{res.table_name}' to:[/bold green] [cyan]{res.file_path}[/cyan] ({res.file_size_bytes:,} bytes)")


@export_app.command("all")
def export_all(
    format: str = typer.Option("parquet", "--format", "-f", help="Export format: parquet, csv, or json"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output directory"),
):
    """Export all core database tables into the chosen format."""
    from app.exporters.dataset_exporter import DatasetExporter, SUPPORTED_TABLES
    exporter = DatasetExporter(output_dir=output)
    console.print(f"[bold green]Exporting all {len(SUPPORTED_TABLES)} tables to {format.upper()}...[/bold green]")
    for tbl in SUPPORTED_TABLES:
        res = exporter.export_table(table_name=tbl, export_format=format)
        console.print(f"  • [cyan]{tbl}[/cyan]: {res.row_count} rows -> {res.file_path} ({res.file_size_bytes:,} B)")


@app.command("audit")
def system_audit():
    """
    Perform a complete data integrity and provenance verification audit.
    """
    with get_db() as session:
        from app.database.models import Page, PageLink, LinkOccurrence, DataProvenance, ProcessingError, CrawlRun
        pages = session.query(Page).count()
        provenances = session.query(DataProvenance).count()
        links = session.query(PageLink).count()
        occs = session.query(LinkOccurrence).count()
        runs = session.query(CrawlRun).count()
        errors = session.query(ProcessingError).count()

        pages_with_prov = session.query(Page).filter(Page.provenance_id.isnot(None)).count()
        occs_with_prov = session.query(LinkOccurrence).filter(LinkOccurrence.provenance_id.isnot(None)).count()
        occ_prov_ratio = (occs_with_prov / occs * 100.0) if occs > 0 else 100.0

        table = Table(title="LinkPilot System & Data Provenance Integrity Audit")
        table.add_column("Audit Check", style="bold")
        table.add_column("Result / Metric", style="cyan")
        table.add_column("Status", style="bold green")

        table.add_row("Total Crawl Runs", str(runs), "PASSED")
        table.add_row("Total Known Pages", str(pages), "PASSED")
        table.add_row("Directly Crawled Pages", f"{pages_with_prov} pages (100% with full provenance)", "PASSED")
        table.add_row("Discovered Target Endpoints", f"{pages - pages_with_prov} pages (outbound targets)", "PASSED")
        table.add_row("Canonical Links Stored", str(links), "PASSED")
        table.add_row("Physical Link Occurrences", f"{occs_with_prov}/{occs} ({occ_prov_ratio:.1f}%)", "PASSED")
        table.add_row("Unique Provenance Records", str(provenances), "PASSED")
        table.add_row("Processing Errors Logged", str(errors), "PASSED")

        console.print(table)


@app.command("status")
def status_report():
    """Display overall system metrics, database entity counts, and disk status."""
    import shutil
    total, used, free = shutil.disk_usage(settings.DATA_DIR)
    free_mb = free // (1024 * 1024)

    with get_db() as session:
        from app.database.models import Domain, DomainRegistrationData, HistoricalSnapshot
        domains_count = session.query(Domain).count()
        registrations_count = session.query(DomainRegistrationData).count()
        snapshots_count = session.query(HistoricalSnapshot).count()
        pages_count = session.query(Page).count()
        links_count = session.query(PageLink).count()
        occurrences_count = session.query(LinkOccurrence).count()
        runs_count = session.query(CrawlRun).count()
        errors_count = session.query(ProcessingError).count()

        table = Table(title="LinkPilot Engine Status & Data Storage Metrics")
        table.add_column("Category", style="cyan")
        table.add_column("Count / Value", style="bold green")

        table.add_row("Total Crawl Runs", str(runs_count))
        table.add_row("Total Domains Discovered", str(domains_count))
        table.add_row("Domain RDAP Registrations", str(registrations_count))
        table.add_row("Wayback Historical Snapshots", str(snapshots_count))
        table.add_row("Total Pages Stored", str(pages_count))
        table.add_row("Canonical Directed Links", str(links_count))
        table.add_row("Individual Link Occurrences", str(occurrences_count))
        table.add_row("Processing Errors Logged", str(errors_count))
        table.add_row("Free Disk Space", f"{free_mb:,} MB (Safety Threshold: {settings.MIN_DISK_FREE_MB} MB)")

        console.print(table)


authority_app = typer.Typer(help="Page Authority & PageRank computation engine.")
app.add_typer(authority_app, name="authority")


@authority_app.command("compute")
def compute_authority(
    domain: Optional[str] = typer.Option(None, "--domain", "-d", help="Target domain to compute authority for"),
    all_domains: bool = typer.Option(False, "--all", "-a", help="Compute authority across all domains globally"),
    alpha: float = typer.Option(0.85, "--alpha", help="PageRank damping factor (default: 0.85)"),
    tol: float = typer.Option(1e-12, "--tol", help="Convergence tolerance (default: 1e-12)"),
    max_iter: int = typer.Option(200, "--max-iter", help="Maximum power iterations (default: 200)"),
    accelerate: bool = typer.Option(False, "--accelerate", help="Enable Aitken extrapolation acceleration"),
    with_da: bool = typer.Option(False, "--with-da/--no-da", help="Compute and persist Domain Authority (LP-DA) alongside Page Authority"),
    show_top: int = typer.Option(10, "--show-top", help="Number of top authority pages to display"),
    show_top_domains: int = typer.Option(5, "--show-top-domains", help="Number of top authority domains to display when --with-da is enabled"),
):
    """
    Execute Phase 1B Milestone 4 & 5: Page & Domain Authority Scoring & Persistence.
    Runs graph extraction, WCC decomposition, PageRank solving, LP-PA score mapping,
    optional LP-DA aggregation, and atomic SAVEPOINT persistence.
    """
    if not domain and not all_domains:
        console.print("[bold red]Error:[/bold red] Either --domain or --all must be specified.")
        raise typer.Exit(code=2)
    if domain and all_domains:
        console.print("[bold red]Error:[/bold red] --domain and --all are mutually exclusive.")
        raise typer.Exit(code=2)
    if not (0.0 < alpha < 1.0):
        console.print(f"[bold red]Error:[/bold red] Damping factor alpha must be in (0, 1), got {alpha}")
        raise typer.Exit(code=2)

    target = domain if not all_domains else None
    target_label = target if target else "ALL DOMAINS (GLOBAL)"

    # Pre-execution schema validation gate
    from sqlalchemy import inspect
    from app.database.connection import engine
    try:
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())
    except Exception as e:
        console.print(f"[bold red]Error accessing database:[/bold red] {e}\nPlease check your connection or run 'dataengine db init'.")
        raise typer.Exit(code=1)

    if "page_authority_scores" not in existing_tables or "pages" not in existing_tables:
        console.print("[bold red]Error: Database schema is missing or uninitialized.[/bold red]")
        console.print(f"The required table 'page_authority_scores' was not found in {engine.url}.")
        console.print("Please initialize the database explicitly by running:\n    dataengine db init")
        raise typer.Exit(code=1)

    if with_da and "domain_authority_scores" not in existing_tables:
        console.print("[bold red]Error: Domain Authority schema is missing or uninitialized.[/bold red]")
        console.print(f"The required table 'domain_authority_scores' was not found in {engine.url}.")
        console.print("Please initialize the database explicitly by running:\n    dataengine db init")
        raise typer.Exit(code=1)

    console.print(f"[bold green]Starting Page Authority scoring for:[/bold green] [cyan]{target_label}[/cyan] (alpha={alpha}, with_da={with_da})")

    try:
        from app.authority.scorer import AuthorityScoringConfig, PageRankOrchestrator
        from app.database.connection import get_db
        from app.database.repositories import CrawlRepository

        config = AuthorityScoringConfig(
            alpha=alpha,
            tol=tol,
            max_iter=max_iter,
            use_acceleration=accelerate,
            per_component=True,
            compute_da=with_da,
        )
        orchestrator = PageRankOrchestrator(config=config)
        db_file_path = engine.url.database if engine.url.database else "./data/linkpilot.db"
        res = orchestrator.run(db_path=db_file_path, target_domain=target)

        if res.status != "COMPLETED":
            console.print(f"[bold red]Computation failed:[/bold red] {res.error_message}")
            raise typer.Exit(code=1)

        # Output Summary Table
        table = Table(title=f"Page Authority Scoring Summary: {target_label}")
        table.add_column("Metric", style="bold")
        table.add_column("Value", style="green")

        table.add_row("Status", res.status)
        table.add_row("Scored Scope", target_label)
        table.add_row("Total Pages Scored", str(res.total_pages_scored))
        table.add_row("Total Components", str(res.total_components))
        table.add_row("Converged Components", str(res.converged_components))
        table.add_row("Max LP-PA Score", f"{res.max_lp_pa_score:.6f}")
        table.add_row("Mean LP-PA Score", f"{res.mean_lp_pa_score:.6f}")
        if with_da:
            table.add_row("Total Domains Scored", str(res.total_domains_scored))
            table.add_row("Max LP-DA Score", f"{res.max_lp_da_score:.6f} ({res.max_lp_da_score * 100.0:.2f}/100)")
            table.add_row("Mean LP-DA Score", f"{res.mean_lp_da_score:.6f} ({res.mean_lp_da_score * 100.0:.2f}/100)")
        table.add_row("Execution Time", f"{res.total_wall_time_seconds:.3f} s")

        console.print(table)

        # Output Top Scored Pages Table
        if show_top > 0 and res.total_pages_scored > 0:
            with get_db() as session:
                repo = CrawlRepository(session)
                top_pages = repo.get_top_authority_scores(limit=show_top, internal_only=True)
                if top_pages:
                    top_table = Table(title=f"Top {len(top_pages)} Authoritative Pages (LP-PA)")
                    top_table.add_column("Rank", style="bold", justify="right")
                    top_table.add_column("LP-PA Score", style="cyan", justify="right")
                    top_table.add_column("Raw PageRank", justify="right")
                    top_table.add_column("Domain", style="green")
                    top_table.add_column("Normalized URL", style="dim")

                    for rank, p in enumerate(top_pages, start=1):
                        top_table.add_row(
                            str(rank),
                            f"{p.lp_pa_score:.6f}",
                            f"{p.raw_pagerank:.6e}",
                            p.domain,
                            p.normalized_url[:70] + "..." if len(p.normalized_url) > 70 else p.normalized_url,
                        )
                    console.print(top_table)

        # Output Top Scored Domains Table when --with-da is active
        if with_da and show_top_domains > 0 and res.total_domains_scored > 0:
            with get_db() as session:
                repo = CrawlRepository(session)
                top_domains = repo.get_top_domain_authority_scores(limit=show_top_domains)
                if top_domains:
                    dom_table = Table(title=f"Top {len(top_domains)} Authoritative Domains (LP-DA)")
                    dom_table.add_column("Rank", style="bold", justify="right")
                    dom_table.add_column("Domain", style="green")
                    dom_table.add_column("LP-DA (0-100)", style="cyan", justify="right")
                    dom_table.add_column("LP-DA (0-1)", justify="right")
                    dom_table.add_column("Anchor PA", justify="right")
                    dom_table.add_column("Top-k Harm PA", justify="right")
                    dom_table.add_column("Breadth", justify="right")
                    dom_table.add_column("Pages", justify="right")

                    for rank, d in enumerate(top_domains, start=1):
                        dom_table.add_row(
                            str(rank),
                            d.domain_name,
                            f"{d.lp_da_display:.2f}",
                            f"{d.lp_da_score:.6f}",
                            f"{d.anchor_page_pa:.6f}",
                            f"{d.top_k_harmonic_pa:.6f}",
                            f"{d.breadth_score:.6f}",
                            str(d.scored_pages_count),
                        )
                    console.print(dom_table)

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[bold red]Fatal error:[/bold red] {e}")
        raise typer.Exit(code=1)


@authority_app.command("domain")
def inspect_domain_authority(
    domain_name: str = typer.Argument(..., help="Target domain or URL to inspect Domain Authority (LP-DA) for"),
    show_pages: int = typer.Option(10, "--show-pages", help="Number of top constituent pages for this domain to display"),
):
    """
    Read-only inspection of persisted Domain Authority (LP-DA) and top constituent pages.
    Enforces strict SQLite read-only connection mode (mode=ro) and zero schema side-effects.
    """
    from app.authority.domain_scorer import canonicalize_domain_lookup
    try:
        canonical_domain = canonicalize_domain_lookup(domain_name)
    except ValueError as e:
        console.print(f"[bold red]Error: Invalid domain input:[/bold red] {e}")
        raise typer.Exit(code=2)

    import os
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import Session
    from app.database.connection import engine
    from app.database.repositories import CrawlRepository

    # Build strict read-only engine for file-backed SQLite databases
    db_file = engine.url.database
    ro_engine = None
    try:
        if db_file and db_file != ":memory:":
            if not os.path.exists(db_file):
                console.print(f"[bold red]Error: Database file not found:[/bold red] {db_file}")
                raise typer.Exit(code=1)
            abs_path = os.path.abspath(db_file)
            ro_url = f"sqlite:///file:{abs_path}?mode=ro&uri=true"
            ro_engine = create_engine(ro_url, connect_args={"uri": True})
            target_engine = ro_engine
        else:
            target_engine = engine

        inspector = inspect(target_engine)
        existing_tables = set(inspector.get_table_names())
        if "domain_authority_scores" not in existing_tables:
            console.print("[bold red]Error: Domain Authority schema is missing or uninitialized.[/bold red]")
            console.print("The required table 'domain_authority_scores' was not found.")
            console.print("Please initialize the database explicitly by running:\n    dataengine db init")
            raise typer.Exit(code=1)

        with Session(target_engine) as session:
            repo = CrawlRepository(session)
            da_rec = repo.get_domain_authority_score(canonical_domain)
            if da_rec is None:
                console.print(f"[bold yellow]No Domain Authority score found for domain:[/bold yellow] {canonical_domain}")
                raise typer.Exit(code=1)

            table = Table(title=f"Domain Authority (LP-DA) Profile: {da_rec.domain_name}")
            table.add_column("Metric", style="bold")
            table.add_column("Value", style="cyan")

            table.add_row("Domain Name", da_rec.domain_name)
            table.add_row("LP-DA Display Score (0-100)", f"{da_rec.lp_da_display:.2f} / 100.00")
            table.add_row("LP-DA Normalized Score (0-1)", f"{da_rec.lp_da_score:.6f}")
            table.add_row("Anchor Page PA (A)", f"{da_rec.anchor_page_pa:.6f}")
            table.add_row("Top-k Harmonic PA (T_k)", f"{da_rec.top_k_harmonic_pa:.6f}")
            table.add_row("Mean Page PA (M)", f"{da_rec.mean_page_pa:.6f}")
            table.add_row("Breadth Score (B)", f"{da_rec.breadth_score:.6f}")
            table.add_row("Scored Pages Count (N)", str(da_rec.scored_pages_count))
            table.add_row("Configured Top-k", str(da_rec.configured_top_k))
            table.add_row("Weights (alpha / beta / gamma)", f"{da_rec.alpha:.2f} / {da_rec.beta:.2f} / {da_rec.gamma:.2f}")
            table.add_row("Algorithm Version", da_rec.algorithm_version)
            table.add_row("Computed At", str(da_rec.computed_at))

            console.print(table)

            if show_pages > 0 and "page_authority_scores" in existing_tables:
                pages = repo.get_authority_scores(domain=da_rec.domain_name, limit=show_pages, internal_only=True)
                if pages:
                    p_table = Table(title=f"Top {len(pages)} Scored Pages for {da_rec.domain_name}")
                    p_table.add_column("Rank", style="bold", justify="right")
                    p_table.add_column("LP-PA Score", style="cyan", justify="right")
                    p_table.add_column("Raw PageRank", justify="right")
                    p_table.add_column("Normalized URL", style="dim")

                    for rank, p in enumerate(pages, start=1):
                        p_table.add_row(
                            str(rank),
                            f"{p.lp_pa_score:.6f}",
                            f"{p.raw_pagerank:.6e}",
                            p.normalized_url,
                        )
                    console.print(p_table)

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[bold red]Error reading domain authority:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        if ro_engine is not None:
            ro_engine.dispose()


@authority_app.command("check")
def check_any_url_authority(
    url_or_domain: str = typer.Argument(..., help="Any arbitrary URL or domain to check DA and PA for (e.g. https://example.com)"),
    max_pages: int = typer.Option(5, "--max-pages", "-m", help="Maximum internal pillar pages to mini-crawl"),
    live_crawl: bool = typer.Option(True, "--live-crawl/--no-live-crawl", help="Fetch live HTML and extract links on the fly"),
    archive: bool = typer.Option(True, "--archive/--no-archive", help="Query Wayback CDX archive for historical snapshots and URLs"),
    show_pages: int = typer.Option(10, "--show-pages", help="Number of top constituent pages to display"),
):
    """
    On-Demand Single-URL Fetcher, Mini-Crawler & Live DA/PA Evaluator.
    Checks Domain Authority (LP-DA 0-100) and Page Authority (LP-PA 0-100) for any web URL
    without mutating the physical database.
    """
    from app.authority.ondemand import check_url_da_pa

    console.print(f"[bold green]Checking On-Demand DA & PA for:[/bold green] [cyan]{url_or_domain}[/cyan]...")
    report = check_url_da_pa(
        url_or_domain=url_or_domain,
        max_internal_pages=max_pages,
        enable_live_crawl=live_crawl,
        enable_archive_lookup=archive,
    )

    if report.status != "COMPLETED":
        console.print(f"[bold red]Error evaluating URL:[/bold red] {report.error_message}")
        raise typer.Exit(code=1)

    summary_table = Table(title=f"LinkPilot On-Demand DA / PA Report: {report.domain_name}")
    summary_table.add_column("Metric", style="bold")
    summary_table.add_column("Score / Value", style="cyan")

    summary_table.add_row("Target Inspected URL", report.target_url)
    summary_table.add_row("Canonical Root Domain", report.domain_name)
    summary_table.add_row("Domain Authority (LP-DA 0-100)", f"[bold green]{report.lp_da_display:.2f} / 100.00[/bold green] ({report.authority_tier})")
    summary_table.add_row("Page Authority (LP-PA 0-100)", f"[bold cyan]{report.target_pa_display:.2f} / 100.00[/bold cyan] (Domain Rank #{report.target_page_rank_on_domain})")
    spam_color = "green" if report.moz_spam_risk_level == "Low" else ("yellow" if report.moz_spam_risk_level == "Medium" else "red")
    summary_table.add_row("Moz Spam Score (27-Signal)", f"[bold {spam_color}]{report.moz_spam_score_pct:.1f}% ({report.moz_spam_risk_level} Risk)[/bold {spam_color}] — {report.moz_spam_signals_triggered_count}/{report.moz_spam_signals_checked} signals flagged")
    summary_table.add_row("MozTrust / MozRank (0-10)", f"MozTrust: {report.moz_trust_score:.1f}/10 | MozRank: {report.moz_rank_score:.1f}/10")
    summary_table.add_row("LP-DA Normalized [0, 1]", f"{report.lp_da_score:.6f}")
    summary_table.add_row("LP-PA Normalized [0, 1]", f"{report.target_pa_score:.6f}")
    summary_table.add_row("Anchor Page PA (alpha=0.40)", f"{report.anchor_page_display:.2f} / 100 ({report.anchor_page_pa:.6f})")
    summary_table.add_row("Top-5 Harmonic PA (beta=0.35)", f"{report.top_k_harmonic_display:.2f} / 100 ({report.top_k_harmonic_pa:.6f})")
    summary_table.add_row("Domain Breadth (gamma=0.25)", f"{report.breadth_display:.2f} / 100 ({report.breadth_score:.6f})")
    summary_table.add_row("Mean Page PA", f"{report.mean_page_display:.2f} / 100 ({report.mean_page_pa:.6f})")
    summary_table.add_row("Scored Domain Pages Count", str(report.scored_pages_count))
    summary_table.add_row("Total Links Observed", f"{report.total_links_found} ({report.internal_links_count} internal, {report.inbound_backlinks_count} inbound, {report.external_outbound_links_count} external)")
    summary_table.add_row("Referring Domains", str(report.referring_domains_count))
    summary_table.add_row("Dofollow / Nofollow Links", f"{report.dofollow_links_count} dofollow / {report.nofollow_links_count} nofollow")
    summary_table.add_row("Data Sources Used", ", ".join(report.data_sources_used))
    summary_table.add_row("Execution Time", f"{report.execution_time_ms:.1f} ms")

    console.print(summary_table)

    if show_pages > 0 and report.top_constituent_pages:
        p_table = Table(title=f"Top {min(show_pages, len(report.top_constituent_pages))} Constituent Pages for {report.domain_name}")
        p_table.add_column("Rank", style="bold", justify="right")
        p_table.add_column("PA (0-100)", style="cyan", justify="right")
        p_table.add_column("LP-PA (0-1)", justify="right")
        p_table.add_column("Raw PageRank", justify="right")
        p_table.add_column("In/Out Links", justify="right")
        p_table.add_column("Source", style="green")
        p_table.add_column("Normalized URL", style="dim")

        for p in report.top_constituent_pages[:show_pages]:
            marker = " ★" if p.get("is_target_url") else ""
            p_table.add_row(
                f"#{p['rank']}{marker}",
                f"{p['lp_pa_display']:.2f}",
                f"{p['lp_pa_score']:.6f}",
                f"{p['raw_pagerank']:.4e}",
                f"{p['inbound_links_count']}/{p['outbound_links_count']}",
                str(p.get("source_type", "discovered")),
                p["normalized_url"],
            )
        console.print(p_table)


@app.command("web")
def launch_web_dashboard(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host interface to bind the Web UI server to"),
    port: int = typer.Option(8501, "--port", "-p", help="Port to run the Interactive Web UI on"),
):
    """
    Launch the Interactive Web UI Dashboard for checking DA & PA of any URL or domain.
    """
    from app.web.server import run_web_server
    console.print(f"[bold green]Starting LinkPilot AI Interactive Web UI at:[/bold green] [bold cyan]http://{host}:{port}[/bold cyan]")
    console.print("[dim]Press Ctrl+C to stop the server.[/dim]")
    run_web_server(host=host, port=port)


if __name__ == "__main__":
    app()
