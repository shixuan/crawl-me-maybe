"""Extract page text with trafilatura and a BeautifulSoup fallback."""

from __future__ import annotations

import datetime
import hashlib
import json
import warnings
from typing import Protocol

import trafilatura
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from crawlme.digest.lxml import LXML_LOCK
from crawlme.schemas import ExtractionStatus, FetchResult, Page


class Extractor(Protocol):
    """Contract for HTML content extraction."""

    def extract(self, fetch_result: FetchResult, raw_html_path: str = "") -> Page: ...


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class TrafExtractor:
    def extract(self, fetch_result: FetchResult, raw_html_path: str = "") -> Page:
        # trafilatura parses with libxml2 all the way through, so the
        # whole body runs under the shared lock (see digest/lxml.py).
        with LXML_LOCK:
            return self._extract(fetch_result, raw_html_path)

    def _extract(self, fetch_result: FetchResult, raw_html_path: str = "") -> Page:
        html_bytes = fetch_result.raw
        html_str = _decode(html_bytes)

        title, published_at = _extract_head_meta(html_str)
        markdown = None
        plain_text = None
        metadata: dict[str, str] = {}
        status: ExtractionStatus = "OK"

        # Primary path: trafilatura handles boilerplate removal, markdown
        # conversion, and metadata extraction in one pass.
        try:
            doc = trafilatura.extract(
                html_str,
                output_format="xml",
                include_tables=True,
                include_images=False,
                include_links=False,
                with_metadata=True,
            )
            if doc is not None:
                markdown = trafilatura.extract(
                    html_str,
                    output_format="markdown",
                    include_tables=True,
                    include_images=False,
                    include_links=False,
                )
                plain_text = trafilatura.extract(
                    html_str,
                    output_format="txt",
                    include_tables=True,
                    include_images=False,
                    include_links=False,
                )
        except Exception:
            status = "DEGRADED"

        # Fallback: pull title and basic text from BeautifulSoup when
        # trafilatura couldn't get anything useful.
        if markdown is None or plain_text is None:
            try:
                soup = BeautifulSoup(html_str, "lxml")
                if title is None:
                    title_tag = soup.find("title")
                    if title_tag:
                        title = title_tag.get_text(strip=True) or None
                if plain_text is None:
                    plain_text = soup.get_text(separator="\n", strip=True)
                if markdown is None:
                    markdown = plain_text
                if status == "OK":
                    status = "DEGRADED"
            except Exception:
                status = "FAILED"

        if title is None:
            title = fetch_result.url.canonical

        text_blob = plain_text or ""
        text_hash = hashlib.sha256(text_blob.encode()).hexdigest()[:16]
        text_len = len(text_blob)

        return Page(
            url_key=fetch_result.url_key,
            url=fetch_result.url,
            raw_html_path=raw_html_path,
            title=title,
            markdown=markdown,
            plain_text=plain_text,
            metadata=metadata,
            text_hash=text_hash,
            text_len=text_len,
            published_at=published_at,
            extracted_at=_utcnow(),
            extraction_status=status,
        )


# Where pages claim their publication time, most trustworthy first.
_DATE_META = (
    ("property", "article:published_time"),
    ("property", "og:published_time"),
    ("property", "article:modified_time"),
    ("name", "date"),
    ("name", "pubdate"),
    ("name", "publish_date"),
    ("name", "publication_date"),
    ("name", "DC.date.issued"),
    ("itemprop", "datePublished"),
)

# Formats seen in the wild that fromisoformat cannot take on 3.10.
_DATE_FORMATS = ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d %B %Y", "%B %d, %Y")


def _extract_head_meta(html_str: str) -> tuple[str | None, datetime.datetime | None]:
    """Read the title and declared publication time from one HTML parse."""
    try:
        with warnings.catch_warnings():
            # Feeds may reach this HTML metadata parser; suppress the expected XML warning.
            warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(html_str, "lxml")
    except Exception:
        return None, None
    title_tag = soup.find("title")
    title = (title_tag.get_text(strip=True) or None) if title_tag else None
    return title, _published_at_from(soup)


def _published_at_from(soup: BeautifulSoup) -> datetime.datetime | None:
    """Read publication time from metadata, JSON-LD or time elements.

    Do not use trafilatura date inference, which can mistake footer dates for publication."""
    for attr, value in _DATE_META:
        tag = soup.find("meta", attrs={attr: value})
        if tag is not None:
            parsed = _parse_date(str(tag.get("content") or ""))
            if parsed is not None:
                return parsed

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        parsed = _parse_date(_jsonld_date(script.string or ""))
        if parsed is not None:
            return parsed

    for tag in soup.find_all("time"):
        parsed = _parse_date(str(tag.get("datetime") or tag.get_text(strip=True)))
        if parsed is not None:
            return parsed
    return None


def _jsonld_date(blob: str) -> str:
    """Pull datePublished out of a JSON-LD block, however it is nested."""
    try:
        data = json.loads(blob)
    except Exception:
        return ""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            found = node.get("datePublished") or node.get("dateCreated")
            if isinstance(found, str):
                return found
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return ""


def _parse_date(raw: str) -> datetime.datetime | None:
    """Parse a UTC timestamp; reject years before 1990 or more than a year ahead."""
    text = (raw or "").strip()
    if not text:
        return None
    candidates = [text.replace("Z", "+00:00") if text.endswith("Z") else text]
    parsed: datetime.datetime | None = None
    try:
        parsed = datetime.datetime.fromisoformat(candidates[0])
    except ValueError:
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    parsed = parsed.astimezone(datetime.timezone.utc)
    now = _utcnow()
    if parsed.year < 1990 or parsed > now + datetime.timedelta(days=365):
        return None
    return parsed


def _decode(html_bytes: bytes) -> str:
    for encoding in ("utf-8", "latin-1", "cp1252"):
        try:
            return html_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return html_bytes.decode("utf-8", errors="replace")
