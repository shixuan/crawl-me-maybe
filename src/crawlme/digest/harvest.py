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
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from crawlme.digest.feed.base import FeedAdapter
from crawlme.digest.links import extract_links
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.schemas import Candidate, Page, Payload

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


class FeedHarvester:
    """A listing yields item permalinks; an item yields nothing.

    The asymmetry is the point, and it holds on every platform. A listing
    is cheap and weak: it carries permalinks and whatever the platform
    generates as alt text, but not what any item says. An item page is
    expensive and strong, and it is a leaf because its text is the thing
    being looked for, not a pointer to it.

    Items that merely tagged the account are kept but marked, because
    someone writing about a shop is often more specific than the shop is,
    while conflating the two would let one monitored account's results
    bleed into another's.

    Everything platform-shaped is asked of the adapter, so adding a
    platform is a new adapter and a line in the factory's registry, not
    another copy of this flow.
    """

    def __init__(self, adapter: FeedAdapter, canonicalizer: Canonicalizer) -> None:
        self._adapter = adapter
        # Same normalization every other source gets.  A permalink taken
        # at face value would carry the raw URL as its url_key while the
        # rest of the crawl keys on a fingerprint, so the same item
        # reached from a link and from a listing would not dedup.
        self._canonicalizer = canonicalizer

    def harvest(self, page: Page, depth: int) -> list[Candidate]:
        if page.url.reg_domain != self._adapter.DOMAIN:
            # A crawl can wander off the platform: an analyzer endorses a
            # shop's own site, and that page arrives here. It is a leaf by
            # policy rather than by luck — the adapter's patterns would
            # mostly fail to match, but a site that happens to use the
            # platform's path shape would otherwise yield candidates
            # pointing at the wrong host entirely.
            logger.debug("harvest.off_platform url=%s platform=%s", page.url.canonical, self._adapter.PLATFORM)
            return []

        html = _html_of(page)
        problem = self._adapter.problem(html)
        if problem is not None:
            # Not an empty page: a page that is not content at all. Saying
            # so is what stops a renamed account reading as a quiet one.
            logger.warning("harvest.not_content url=%s problem=%s", page.url.canonical, problem.value)
            return []
        if self._adapter.parse_item(html, page.url.canonical) is not None:
            return []

        listing = self._adapter.parse_listing(html, page.url.canonical, _payloads_of(page))
        own = {i.permalink for i in listing.own}
        out: list[Candidate] = []
        for item in listing.all:
            marked = replace(item, signals={**item.signals, "tagged_only": item.permalink not in own})
            candidate = marked.to_candidate(source_url_key=page.url_key, depth=depth + 1)
            candidate.url = self._canonicalizer.canonicalize(candidate.url.raw, page.url.canonical)
            out.append(candidate)
        return out


def _html_of(page: Page) -> str:
    """Read back the page bytes the extractor was given.

    The harvester needs the markup, not the prose an extractor distilled
    out of it, so it reads the frozen copy rather than page.markdown.
    """
    if not page.raw_html_path:
        return ""
    try:
        return Path(page.raw_html_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("harvest.raw_unreadable path=%s", page.raw_html_path)
        return ""


def _payloads_of(page: Page) -> list[Payload]:
    """Read back what the page fetched for itself, in arrival order.

    Same reasoning as the raw markup: the frozen copy is what a parser
    reruns against, so a change to it can be judged on exactly what
    arrived rather than on a fresh request.
    """
    out: list[Payload] = []
    for path in page.payload_paths:
        try:
            out.append(Payload(url="", content_type="", body=Path(path).read_bytes()))
        except OSError:
            logger.warning("harvest.payload_unreadable path=%s", path)
    return out


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
