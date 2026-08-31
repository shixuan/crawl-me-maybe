"""Harvest: what candidates does a fetched page yield?

The engine used to call the link extractor directly, which quietly meant
"a page yields the links in it". That is true of a link graph and false
of a feed, where a listing yields post permalinks and a post yields
nothing at all, because a post is a leaf whose content is the product.

Which reading applies is a question about the page, not about the run,
so the page is offered to each adapter and the first to claim it does
the reading. Nobody claiming is the ordinary case and the graph's
answer: read the links. The engine still owns everything around it:
pre-filtering, buffering, persistence and counters are the same either
way.

Parsing is deliberately sync so the engine can keep running it in a
worker thread under a timeout, the way a pathological page has to lose
its links rather than stall the crawl.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from crawlme.digest.feed.base import FeedAdapter, PageProblem
from crawlme.digest.links import extract_links
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.schemas import Candidate, Page, Payload

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Harvest:
    """What a page yielded, and why it yielded nothing when it did.

    An empty list used to be the only thing a harvester could say, which
    made "this account posted nothing this week" and "the platform
    refused us" the same answer.  A run that cannot tell them apart
    reports being blocked as a quiet week, every week.
    """

    candidates: list[Candidate]
    problem: PageProblem | None = None
    # The rest of a paged listing, enqueued at this page's own depth: it
    # is more of the same listing, not a hop away from it.
    next_url: str = ""
    # Whether this came from a page that lists other pages.  Only a
    # listing can be judged empty or not: an item page yields nothing by
    # design, and a link graph has no listings at all.  Declared by the
    # harvester, which knows, rather than inferred by the caller, which
    # would be guessing from the shape of the candidates.
    listing: bool = False


class Harvester(Protocol):
    """Turn one fetched page into the candidates it offers."""

    def harvest(self, page: Page, depth: int) -> Harvest: ...


class PageHarvester:
    """One page, read by whichever adapter claims it.

    A page nobody claims is read as a page: its links become candidates,
    which is what a link graph has always done.  That fallback replaced
    an explicit escape hatch inside the feed reader -- "the domain is
    not mine, return nothing" -- which was the same rule stated as an
    exception, and which silently dropped the links of every page a
    crawl wandered onto.

    *adapters* is what this run is allowed to use, in the order they are
    asked.  Order is priority and is decided by the caller: two adapters
    that could both claim a page is a question about the run, not about
    either adapter.
    """

    def __init__(self, canonicalizer: Canonicalizer, adapters: Sequence[FeedAdapter] = ()) -> None:
        # Same normalization every other source gets.  A permalink taken
        # at face value would carry the raw URL as its url_key while the
        # rest of the crawl keys on a fingerprint, so the same item
        # reached from a link and from a listing would not dedup.
        self._canonicalizer = canonicalizer
        self._adapters = tuple(adapters)

    def harvest(self, page: Page, depth: int) -> Harvest:
        # Read once and hand it around: one adapter answers from the
        # host and never looks at it, the other can only answer from it.
        document = _html_of(page) if self._adapters else ""
        for adapter in self._adapters:
            if adapter.claims(page, document):
                return self._from_adapter(adapter, page, document, depth)
        return self._from_links(page, depth)

    def _from_links(self, page: Page, depth: int) -> Harvest:
        """A page yields the links in it: the graph traversal's reading."""
        base = page.url.canonical
        out = [
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
            for raw in extract_links(page)
        ]
        # A link graph has no notion of a page that refuses to be one:
        # a wall is just a page with no links on it.
        return Harvest(out)

    def _from_adapter(self, adapter: FeedAdapter, page: Page, document: str, depth: int) -> Harvest:
        """A listing yields item permalinks; an item yields nothing.

        The asymmetry is the point, and it holds on every platform. A
        listing is cheap and weak: it carries permalinks and whatever the
        platform generates as alt text, but not what any item says. An
        item page is expensive and strong, and it is a leaf because its
        text is the thing being looked for, not a pointer to it.

        Items that merely tagged the account are kept but marked, because
        someone writing about a shop is often more specific than the shop
        is, while conflating the two would let one monitored account's
        results bleed into another's.
        """
        html = document
        problem = adapter.problem(html)
        if problem is not None:
            # Not an empty page: a page that is not content at all. Saying
            # so is what stops a renamed account reading as a quiet one.
            logger.warning("harvest.not_content url=%s problem=%s", page.url.canonical, problem.value)
            return Harvest([], problem)
        if adapter.parse_item(html, page.url.canonical) is not None:
            return Harvest([])

        listing = adapter.parse_listing(html, page.url.canonical, _payloads_of(page))
        next_url = adapter.next_page(html, page.url.canonical)
        own = {i.permalink for i in listing.own}
        out: list[Candidate] = []
        for item in listing.all:
            marked = replace(item, signals={**item.signals, "tagged_only": item.permalink not in own})
            candidate = marked.to_candidate(source_url_key=page.url_key, depth=depth + 1)
            candidate.url = self._canonicalizer.canonicalize(candidate.url.raw, page.url.canonical)
            out.append(candidate)
        if not out:
            logger.warning("harvest.listing_empty url=%s platform=%s", page.url.canonical, adapter.PLATFORM)
        return Harvest(out, listing=True, next_url=next_url)


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
