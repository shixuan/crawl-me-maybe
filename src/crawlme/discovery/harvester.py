"""Discover candidates through the first claiming adapter or ordinary page links."""

from __future__ import annotations

import datetime
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from crawlme.adapters.base import FeedAdapter, PageProblem
from crawlme.discovery.links import extract_links
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.schemas import Candidate, Page, Payload

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Harvest:
    """Candidates, pagination and content-health information from a page."""

    candidates: list[Candidate]
    problem: PageProblem | None = None
    # The rest of a paged listing, enqueued at this page's own depth: it
    # is more of the same listing, not a hop away from it.
    next_url: str = ""
    # Only listings contribute to the empty-listing diagnostic; posts are leaves.
    listing: bool = False
    # The listing was read without the platform's own answer, so it holds
    # whatever the markup had rather than what the account has now.
    degraded: bool = False


class Harvester(Protocol):
    """Turn one fetched page into the candidates it offers."""

    def harvest(self, page: Page, depth: int) -> Harvest: ...


class PageHarvester:
    """Dispatch saved documents to adapters, falling back to web link extraction."""

    def __init__(self, canonicalizer: Canonicalizer, adapters: Sequence[FeedAdapter] = ()) -> None:
        # Canonicalize adapter URLs before deduplicating against ordinary links.
        self._canonicalizer = canonicalizer
        self._adapters = tuple(adapters)

    def harvest(self, page: Page, depth: int) -> Harvest:
        # Read once for all adapter recognition checks.
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
        """Posts are leaves; listings yield candidates, owner flags and optional pagination."""
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
        if listing.degraded:
            # Loudly, because it looks like success: the count is normal,
            # nothing refused us, and the content is weeks old.
            logger.warning(
                "harvest.listing_stale url=%s read from markup alone, its newest posts are missing",
                page.url.canonical,
            )
        return Harvest(out, listing=True, next_url=next_url, degraded=listing.degraded)


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
    """Read saved sub-responses in arrival order, skipping unreadable files."""
    out: list[Payload] = []
    for path in page.payload_paths:
        try:
            out.append(Payload(url="", content_type="", body=Path(path).read_bytes()))
        except OSError:
            logger.warning("harvest.payload_unreadable path=%s", path)
    return out


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
