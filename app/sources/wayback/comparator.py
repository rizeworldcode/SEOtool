"""
Wayback Historical Snapshot Comparator.
Compares two snapshots of a page across time to detect changes in page titles,
meta descriptions, and outbound link additions/removals.
"""
from typing import List, Set, Optional
from pydantic import BaseModel
from app.processors.link_extractor import LinkExtractor
from app.processors.html_processor import HTMLProcessor


class LinkDiff(BaseModel):
    added_links: List[str]
    removed_links: List[str]
    retained_links: List[str]
    total_added: int
    total_removed: int
    total_retained: int


class SnapshotComparisonResult(BaseModel):
    domain_name: str
    target_url: str
    timestamp_earlier: str
    timestamp_later: str
    is_content_identical: bool
    title_earlier: Optional[str] = None
    title_later: Optional[str] = None
    title_changed: bool = False
    meta_desc_earlier: Optional[str] = None
    meta_desc_later: Optional[str] = None
    meta_desc_changed: bool = False
    link_diff: LinkDiff


class WaybackComparator:
    """Compares historical snapshots for SEO evolution analysis."""

    @staticmethod
    def compare_html_snapshots(
        domain_name: str,
        target_url: str,
        ts_earlier: str,
        html_earlier: str,
        ts_later: str,
        html_later: str,
    ) -> SnapshotComparisonResult:
        """Analyze differences between earlier and later HTML versions of a page."""
        meta_earlier = HTMLProcessor.parse_metadata(html_earlier)
        meta_later = HTMLProcessor.parse_metadata(html_later)

        title_changed = meta_earlier.title != meta_later.title
        desc_changed = meta_earlier.meta_description != meta_later.meta_description

        # Extract links
        links_earlier = LinkExtractor.extract_links(html_earlier, target_url)
        links_later = LinkExtractor.extract_links(html_later, target_url)

        urls_earlier: Set[str] = {l.normalized_target_url for l in links_earlier}
        urls_later: Set[str] = {l.normalized_target_url for l in links_later}

        added = sorted(list(urls_later - urls_earlier))
        removed = sorted(list(urls_earlier - urls_later))
        retained = sorted(list(urls_earlier & urls_later))

        link_diff = LinkDiff(
            added_links=added,
            removed_links=removed,
            retained_links=retained,
            total_added=len(added),
            total_removed=len(removed),
            total_retained=len(retained),
        )

        return SnapshotComparisonResult(
            domain_name=domain_name,
            target_url=target_url,
            timestamp_earlier=ts_earlier,
            timestamp_later=ts_later,
            is_content_identical=(html_earlier.strip() == html_later.strip()),
            title_earlier=meta_earlier.title,
            title_later=meta_later.title,
            title_changed=title_changed,
            meta_desc_earlier=meta_earlier.meta_description,
            meta_desc_later=meta_later.meta_description,
            meta_desc_changed=desc_changed,
            link_diff=link_diff,
        )
