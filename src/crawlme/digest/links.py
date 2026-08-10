"""Link extraction.

Parses HTML to find all <a href> tags and produces RawLink records with
anchor text, surrounding snippet, nearest parent heading, and position.

The heading-finding logic first checks ancestors — is this link nested
inside an h1-h6 tag? — then falls back to the most recent preceding heading
in document order via find_previous().  Structural context like "this link
appeared under an 'API Reference' h2" helps the ranker decide whether it's
worth following.

Snippets are the text content of the link's immediate parent element,
truncated to ~200 characters.  They stay compact enough for the ranker's
context window while still carrying enough surrounding text to judge
relevance.
"""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from crawlme.schemas import RawLink

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def extract_links(html: str | bytes) -> list[RawLink]:
    soup = BeautifulSoup(html, "lxml")
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
