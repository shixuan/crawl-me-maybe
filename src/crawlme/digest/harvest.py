"""Harvest: what candidates does a fetched page yield?

The engine used to call the link extractor directly, which quietly meant
"a page yields the links in it". That is true of a link graph and false
of a feed, where a listing yields post permalinks and a post yields
nothing at all, because a post is a leaf whose content is the product.

Two implementations exist now, so the seam is worth naming; before that
it would have been a guess about how the second one differs. The engine
still owns everything around it: pre-filtering, buffering, persistence
and counters are the same either way.

Parsing is deliberately sync so the engine can keep running it in a
worker thread under a timeout, the way a pathological page has to lose
its links rather than stall the crawl.
"""

from __future__ import annotations

import datetime
import logging
from typing import Protocol

from crawlme.digest.feed import instagram
from crawlme.digest.feed.base import FeedItem
from crawlme.digest.links import extract_links
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.schemas import Candidate, Page

logger = logging.getLogger(__name__)


class Harvester(Protocol):
    """Turn one fetched page into the candidates it offers."""

    def harvest(self, page: Page, depth: int) -> list[Candidate]: ...


class LinkHarvester:
    """A page yields the links in it: the graph traversal's reading."""

    def __init__(self, canonicalizer: Canonicalizer) -> None:
        self._canonicalizer = canonicalizer

    def harvest(self, page: Page, depth: int) -> list[Candidate]:
        base = page.url.canonical
        out: list[Candidate] = []
        for raw in extract_links(page):
            out.append(
                Candidate(
                    url=self._canonicalizer.canonicalize(raw.href, base),
                    anchor=raw.anchor,
                    snippet=raw.snippet,
                    parent_heading=raw.parent_heading,
                    position=raw.position,
                    source_url_key=page.url_key,
                    depth=depth + 1,
                    discovered_at=_utcnow(),
                )
            )
        return out


class InstagramHarvester:
    """A listing yields post permalinks; a post yields nothing.

    The asymmetry is the point. A profile grid is cheap and weak: it
    carries permalinks and Instagram's generated alt text, but not what
    any post says. A post page is expensive and strong, and it is a leaf
    because its caption is the thing being looked for, not a pointer to
    it.

    Posts that merely tagged the account are kept but marked, because a
    reviewer writing about a shop is often more specific than the shop
    is, while conflating the two would let one monitored account's
    results bleed into another's.
    """

    def harvest(self, page: Page, depth: int) -> list[Candidate]:
        html = _html_of(page)
        problem = instagram.problem(html)
        if problem is not None:
            # Not an empty page: a page that is not content at all. Saying
            # so is what stops a renamed account reading as a quiet one.
            logger.warning("harvest.not_content url=%s problem=%s", page.url.canonical, problem.value)
            return []
        if instagram.parse_item(html, page.url.canonical) is not None:
            return []

        account = _account_of(page.url.canonical)
        listing = instagram.parse_listing(html, account)
        out: list[Candidate] = []
        for permalink in listing.all:
            item = FeedItem(
                permalink=permalink,
                platform=instagram.PLATFORM,
                signals={"tagged_only": permalink not in listing.own},
            )
            out.append(item.to_candidate(source_url_key=page.url_key, depth=depth + 1))
        return out


def _html_of(page: Page) -> str:
    """Read back the page bytes the extractor was given.

    The harvester needs the markup, not the prose an extractor distilled
    out of it, so it reads the frozen copy rather than page.markdown.
    """
    if not page.raw_html_path:
        return ""
    try:
        from pathlib import Path

        return Path(page.raw_html_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("harvest.raw_unreadable path=%s", page.raw_html_path)
        return ""


def _account_of(url: str) -> str:
    import re

    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)/?", url)
    handle = m.group(1) if m else ""
    return "" if handle in {"p", "reel", "explore"} else handle


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
