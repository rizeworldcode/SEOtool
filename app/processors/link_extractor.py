"""
Link Extractor Module.
Extracts all outbound hyperlinks, anchor texts, rel attributes, DOM contexts,
and positions from HTML documents, preserving exact raw URLs and normalized targets.
"""
from typing import List, Optional
from urllib.parse import urljoin, urlparse, urlunparse
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel


class ExtractedLinkOccurrence(BaseModel):
    raw_target_url: str
    normalized_target_url: str
    anchor_text: Optional[str] = None
    rel_attributes: Optional[str] = None
    link_type: str = "a_href"
    link_position: int = 0
    dom_context: Optional[str] = None
    is_internal: bool = False


def normalize_target_url(raw_url: str, base_url: str) -> str:
    """
    Resolve relative URLs against base_url and normalize scheme and hostname.
    Strips client-side fragment (#frag) but preserves original query parameters.
    """
    # 1. Resolve relative URLs
    resolved = urljoin(base_url, raw_url.strip())
    parsed = urlparse(resolved)

    # 2. Normalize scheme and hostname to lowercase
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    # 3. Strip fragment, keep path and query parameters unchanged
    normalized = urlunparse((scheme, netloc, parsed.path or "/", parsed.params, parsed.query, ""))
    return normalized


def determine_dom_context(tag: Tag) -> str:
    """Identify the nearest structural DOM context of a link by traversing upwards."""
    curr = tag.parent
    priority_contexts = ("nav", "header", "footer", "article", "main", "aside", "section")
    while curr and curr.name:
        tag_name = curr.name.lower()
        if tag_name in priority_contexts:
            return tag_name
        curr = curr.parent
    return "body"


class LinkExtractor:
    """Extracts all outbound links and link occurrences from an HTML document."""

    @staticmethod
    def extract_links(html_content: str, source_url: str) -> List[ExtractedLinkOccurrence]:
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, "lxml")
        occurrences: List[ExtractedLinkOccurrence] = []
        source_parsed = urlparse(source_url)
        source_domain = source_parsed.hostname.lower() if source_parsed.hostname else ""

        # Find all <a> anchor tags with an href attribute
        anchor_tags = soup.find_all("a", href=True)

        for index, tag in enumerate(anchor_tags):
            raw_href = tag["href"].strip()
            
            # Skip empty, javascript, mailto, tel links
            if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
                continue

            normalized_url = normalize_target_url(raw_href, source_url)
            target_parsed = urlparse(normalized_url)
            target_domain = target_parsed.hostname.lower() if target_parsed.hostname else ""

            # Check if internal or external link
            is_internal = (target_domain == source_domain) or target_domain.endswith(f".{source_domain}")

            # Extract anchor text or img alt text
            anchor_text = tag.get_text(strip=True)
            if not anchor_text:
                img = tag.find("img")
                if img and img.get("alt"):
                    anchor_text = f"[image: {img['alt'].strip()}]"

            # Extract rel attributes
            rel = tag.get("rel")
            rel_str = " ".join(rel) if isinstance(rel, list) else (rel or "")

            # Determine DOM context
            dom_context = determine_dom_context(tag)

            occurrences.append(
                ExtractedLinkOccurrence(
                    raw_target_url=raw_href,
                    normalized_target_url=normalized_url,
                    anchor_text=anchor_text or None,
                    rel_attributes=rel_str or None,
                    link_type="a_href",
                    link_position=index,
                    dom_context=dom_context,
                    is_internal=is_internal,
                )
            )

        return occurrences
