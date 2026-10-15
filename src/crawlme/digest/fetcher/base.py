"""Fetcher contract and error facts."""

from __future__ import annotations

from typing import Protocol

from crawlme.digest.models import FetchResult
from crawlme.errors import Error, Kind
from crawlme.pioneer import FrontierItem

# Default crawler identity shared by fetchers and robots policy.
DEFAULT_UA = "crawl-me-maybe (research crawler; +https://github.com/crawl-me-maybe)"


class FetchError(Error):
    """Failure of one fetch attempt."""

    source = "fetch"
    code = "fetch_failed"

    @classmethod
    def from_http(cls, status: int, *, url: str) -> FetchError:
        kind = (
            Kind.UNAVAILABLE
            if status >= 500
            else {
                401: Kind.UNAUTHENTICATED,
                403: Kind.FORBIDDEN,
                404: Kind.NOT_FOUND,
                408: Kind.TIMEOUT,
                410: Kind.NOT_FOUND,
                429: Kind.RATE_LIMITED,
            }.get(status, Kind.INVALID_INPUT)
        )
        return cls(
            f"HTTP request failed: {status}",
            code="http_rejected",
            kind=kind,
            details={"status_code": status, "url": url},
        )


class Fetcher(Protocol):
    """Fetch candidates and release persistent resources through aclose()."""

    async def fetch(self, item: FrontierItem) -> FetchResult: ...

    async def aclose(self) -> None: ...
