"""Shared feed types and the platform adapter contract."""

from __future__ import annotations

from typing import Protocol

from crawlme.digest import FetchResult, Page, Payload
from crawlme.errors import Error, Kind, Scope
from crawlme.platforms.models import FeedItem, Listing, PageProblem


class FeedDependencyError(Error):
    """A missing adapter dependency that prevents further processing of this format."""

    scope = Scope.DEPENDENCY
    source = "platform"
    code = "dependency_missing"
    kind = Kind.NOT_FOUND


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

    def extract_text(self, result: FetchResult) -> str | None:
        """Return target-page text from fetched data, or None for generic HTML extraction."""
        ...
