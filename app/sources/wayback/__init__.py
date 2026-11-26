"""
Wayback Machine Acquisition Module.
"""
from app.sources.wayback.cdx_client import WaybackCDXClient, WaybackCDXRecord
from app.sources.wayback.snapshot_downloader import WaybackSnapshotDownloader, RawSnapshotResult
from app.sources.wayback.comparator import WaybackComparator, SnapshotComparisonResult
from app.sources.wayback.pipeline import WaybackPipeline, WaybackPipelineResult

__all__ = [
    "WaybackCDXClient",
    "WaybackCDXRecord",
    "WaybackSnapshotDownloader",
    "RawSnapshotResult",
    "WaybackComparator",
    "SnapshotComparisonResult",
    "WaybackPipeline",
    "WaybackPipelineResult",
]
