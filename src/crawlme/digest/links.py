"""Link extraction — finds all <a href> in a Page and produces RawLink records.

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

from crawlme.schemas import Page, RawLink

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def extract_links(page: Page) -> list[RawLink]:
    return _extract_from_html(page.raw_html_path)


def _extract_from_html(path: str) -> list[RawLink]:
    if not path:
        return []
    html_bytes = Path(path).read_bytes()
    soup = BeautifulSoup(html_bytes, "lxml")
    links: list[RawLink] = []

    for position, tag in enumerate(soup.find_all("a", href=True), start=1):
        href = tag.get("href", "")
        if isinstance(href, list):
            href = href[0] if href else ""
        href = str(href).strip()
        if not href:
            continue

        anchor = tag.get_text(strip=True) or None
        snippet = _extract_snippet(tag)
        parent_heading = _nearest_heading(tag)

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
        text = parent.get_text(separator=" ", strip=True)
    except Exception:
        return None
    if len(text) > 200:
        return text[:200] + "..."
    return text or None


def _nearest_heading(tag: Tag) -> str | None:
    for parent in tag.parents:
        if getattr(parent, "name", None) in _HEADING_TAGS:
            text = parent.get_text(strip=True)
            if text:
                return text

    prev = tag.find_previous(list(_HEADING_TAGS))
    if prev is not None:
        text = prev.get_text(strip=True)
        if text:
            return text

    return None
