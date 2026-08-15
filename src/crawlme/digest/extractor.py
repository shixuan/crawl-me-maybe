"""HTML content extraction.

Takes a raw HTML FetchResult, strips boilerplate (ads, nav, scripts, comments),
and produces a clean Page with markdown body, plain text, title, and metadata.

Uses trafilatura for the heavy lifting (boilerplate removal + markdown conversion)
with BeautifulSoup as a fallback for title/metadata extraction when the HTML is
malformed enough that trafilatura can't parse it.
"""

from __future__ import annotations

import datetime
import hashlib
from typing import Protocol

import trafilatura
from bs4 import BeautifulSoup

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

        title = None
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
                    output_format="text",
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

        # title from metadata if not found yet.
        if title is None and doc is not None:
            try:
                meta_title = doc.find(".//head//title")
                if meta_title is not None and meta_title.text:  # type: ignore[attr-defined]
                    title = meta_title.text.strip()  # type: ignore[attr-defined]
            except Exception:  # noqa: S110
                pass

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
            extracted_at=_utcnow(),
            extraction_status=status,
        )


def _decode(html_bytes: bytes) -> str:
    for encoding in ("utf-8", "latin-1", "cp1252"):
        try:
            return html_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return html_bytes.decode("utf-8", errors="replace")
