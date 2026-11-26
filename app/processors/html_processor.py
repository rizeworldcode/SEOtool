"""
HTML Metadata Processor.
Parses HTML document using BeautifulSoup to extract title, meta description,
H1/H2 headings, canonical URLs, and language tags.
"""
import json
from typing import Optional, List, Dict
from bs4 import BeautifulSoup
from pydantic import BaseModel


class ParsedHTMLMetadata(BaseModel):
    title: Optional[str] = None
    meta_description: Optional[str] = None
    h1_headings: List[str] = []
    h2_headings: List[str] = []
    canonical_url: Optional[str] = None
    language: Optional[str] = None


class HTMLProcessor:
    """Extracts structured document metadata from HTML strings."""

    @staticmethod
    def parse_metadata(html_content: str) -> ParsedHTMLMetadata:
        if not html_content:
            return ParsedHTMLMetadata()

        soup = BeautifulSoup(html_content, "lxml")

        # 1. Page Title
        title = None
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            title = title_tag.string.strip()

        # 2. Meta Description
        meta_desc = None
        desc_tag = soup.find("meta", attrs={"name": lambda x: x and x.lower() == "description"})
        if not desc_tag:
            desc_tag = soup.find("meta", attrs={"property": "og:description"})
        if desc_tag and desc_tag.get("content"):
            meta_desc = desc_tag["content"].strip()

        # 3. Canonical Link
        canonical_url = None
        canonical_tag = soup.find("link", attrs={"rel": lambda x: x and "canonical" in [r.lower() for r in (x if isinstance(x, list) else [x])]})
        if canonical_tag and canonical_tag.get("href"):
            canonical_url = canonical_tag["href"].strip()

        # 4. H1 & H2 Headings
        h1_headings = [h.get_text(strip=True) for h in soup.find_all("h1") if h.get_text(strip=True)]
        h2_headings = [h.get_text(strip=True) for h in soup.find_all("h2") if h.get_text(strip=True)]

        # 5. Language Tag
        language = None
        html_tag = soup.find("html")
        if html_tag and html_tag.get("lang"):
            language = html_tag["lang"].strip()

        return ParsedHTMLMetadata(
            title=title,
            meta_description=meta_desc,
            h1_headings=h1_headings,
            h2_headings=h2_headings,
            canonical_url=canonical_url,
            language=language,
        )
