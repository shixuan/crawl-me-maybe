"""Link extraction: finds all <a href> in a Page and produces RawLink records.

- anchor: link text, stripped.  None if only contains an image.
- snippet: parent element text truncated to ~200 chars.
- parent_heading: nearest h1-h6 ancestor, or preceding heading in document order.
- Empty/missing hrefs are skipped.

Reads raw HTML from page.raw_html_path on disk so it can parse the original
DOM.  Parsing from plain_text or markdown would lose <a> tags.
"""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup, Tag

from crawlme.digest.lxml import LXML_LOCK
from crawlme.schemas import Page, RawLink

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def extract_links(page: Page) -> list[RawLink]:
    return _extract_from_html(page.raw_html_path)


def _extract_from_html(path: str) -> list[RawLink]:
    if not path:
        return []
    html_bytes = Path(path).read_bytes()
    # The parse and the tree walk both run under the shared lock:
    # libxml2's global dictionary races across threads (digest/lxml.py).
    with LXML_LOCK:
        soup = BeautifulSoup(html_bytes, "lxml")
        links: list[RawLink] = []
        # One document-order pass over headings and links.  The fallback
        # heading for a link with no heading ancestor is the nearest
        # preceding heading, which the pass tracks in O(1).  The previous
        # per-link find_previous rescan was quadratic on pages with many
        # links (a 14k-link page took minutes).
        last_heading: str | None = None
        position = 0

        for tag in soup.find_all(["a", *_HEADING_TAGS]):
            if tag.name in _HEADING_TAGS:
                text = tag.get_text(strip=True)
                if text:
                    last_heading = text
                continue
            if not tag.has_attr("href"):
                continue
            position += 1
            href = tag.get("href", "")
            if isinstance(href, list):
                href = href[0] if href else ""
            href = str(href).strip()
            if not href:
                continue

            anchor = tag.get_text(strip=True) or None
            snippet = _extract_snippet(tag)
            parent_heading = _nearest_heading(tag) or last_heading

            links.append(
                RawLink(
                    href=href,
                    anchor=anchor,
                    snippet=snippet,
                    parent_heading=parent_heading,
                    position=position,
                )
            )

    return links


def _extract_snippet(tag: Tag) -> str | None:
    parent = tag.parent
    if parent is None:
        return None
    try:
        text = _parent_text(parent)
    except Exception:
        return None
    if len(text) > 200:
        return text[:200] + "..."
    return text or None


def _parent_text(parent: Tag, limit: int = 201) -> str:
    """First ~limit chars of the parent's text, walking only as far as
    needed.  get_text() walks the entire subtree, which is quadratic
    when every link shares one giant parent like <body>."""
    parts: list[str] = []
    total = 0
    for s in parent.stripped_strings:
        parts.append(s)
        total += len(s) + 1
        if total >= limit:
            break
    return " ".join(parts)


def _nearest_heading(tag: Tag) -> str | None:
    """Nearest heading ancestor.  Links without one fall back to the
    last heading seen in document order, tracked by the caller's pass."""
    for parent in tag.parents:
        if getattr(parent, "name", None) in _HEADING_TAGS:
            text = parent.get_text(strip=True)
            if text:
                return text
    return None
