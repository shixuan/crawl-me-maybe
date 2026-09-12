"""Shared feed types and the platform adapter contract."""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass, field
from typing import Any, Protocol

from crawlme.schemas import URL, Candidate, Page, Payload


class FeedDependencyError(RuntimeError):
    """A missing adapter dependency that prevents further processing of this format."""


class PageProblem(str, enum.Enum):
    """Content-level failures reported by platform adapters."""

    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    LOGIN_REQUIRED = "login_required"

    @property
    def refuses_the_run(self) -> bool:
        """Treat unavailable pages as local failures and other refusals as run-wide."""
        return self is not PageProblem.UNAVAILABLE


@dataclass(frozen=True)
class FeedItem:
    """Adapter output converted to Candidate at the harvester boundary."""

    permalink: str
    platform: str
    item_id: str = ""
    author: str = ""
    text: str = ""
    published_at: datetime.datetime | None = None
    signals: dict[str, Any] = field(default_factory=dict)

    def to_candidate(self, *, source_url_key: str = "", depth: int = 0) -> Candidate:
        extra: dict[str, Any] = {"platform": self.platform, **self.signals}
        if self.item_id:
            extra["item_id"] = self.item_id
        if self.author:
            extra["account"] = self.author

        # The harvester canonicalizes this URL before enqueueing.
        return Candidate(
            url=URL(
                raw=self.permalink,
                canonical=self.permalink,
                url_key=self.permalink,
                reg_domain=_domain_of(self.permalink),
            ),
            text=self.text,
            posted_at=self.published_at,
            signals=extra,
            source_url_key=source_url_key,
            depth=depth,
            discovered_at=_utcnow(),
        )


@dataclass(frozen=True)
class Listing:
    """Listing entries split between the source owner and other authors."""

    own: list[FeedItem] = field(default_factory=list)
    others: list[FeedItem] = field(default_factory=list)
    # Optional continuation URL for paged listings.
    next_url: str = ""
    # The adapter reports potentially incomplete or stale listing content.
    degraded: bool = False

    @property
    def all(self) -> list[FeedItem]:
        return [*self.own, *self.others]


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class FeedAdapter(Protocol):
    """Platform-specific recognition, failure detection and parsing. No fetching."""

    # Platform name, stamped onto every candidate's signals.
    PLATFORM: str
    # Registrable domain the platform serves, used to tell its own pages
    # from anything a crawl wandered onto.
    DOMAIN: str

    # Nonzero requests scrolling; Settings.feed_scrolls sets the actual limit.
    SCROLLS: int

    # Rendering and authentication are independent adapter requirements.
    NEEDS_RENDERING: bool

    # Require saved login state before enabling this adapter.
    NEEDS_SESSION: bool

    def next_page(self, html: str, url: str) -> str:
        """Return the next listing URL, or an empty string when exhausted."""
        ...

    def claims_url(self, url: str) -> bool:
        """Recognize an address before fetching; return False if the document is needed."""
        ...

    def claims(self, page: Page, document: str) -> bool:
        """Recognize a fetched page from its URL and document."""
        ...

    def problem(self, html: str) -> PageProblem | None:
        """Return a recognized content-level failure, or None if none is detected."""
        ...

    def keeps_payload(self, url: str, content_type: str) -> bool:
        """Select page sub-responses whose content the adapter needs."""
        ...

    def parse_listing(self, html: str, url: str, payloads: list[Payload]) -> Listing:
        """Read listing entries and their owners. Fall back to markup if payloads are absent."""
        ...

    def parse_item(self, html: str, url: str) -> FeedItem | None:
        """Read a single-item page, or None if this is not one."""
        ...
