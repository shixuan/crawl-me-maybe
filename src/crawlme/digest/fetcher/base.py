"""The fetch contract, plus the one behaviour every fetcher shares.

Kept apart from any implementation so a new fetcher does not have to
import the HTTP one to get at the protocol, the error type, or the retry
loop.  Before this split the browser fetcher reached into the httpx
module for a private constant, which is the wrong direction: a contract
should not live inside one of the things it constrains.

Structural typing rather than a base class, matching every other seam in
this codebase (Ranker, CrawlDb, Analyzer, Ordering).  Retry is the only
behaviour the fetchers actually share, and twenty lines of it belong in a
function; a base class would invite shared state to accumulate, and would
break the plain mocks the scheduler tests inject.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from crawlme.schemas import FetchResult, FrontierItem

logger = logging.getLogger(__name__)

#: One Chrome/Win string.  Pass a longer pool in production: a request is
#: fingerprinted on more than this, but a single UA across a whole crawl
#: is the easiest signal to notice.
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"

#: Backoff is capped so a long retry cannot outlive the crawl itself.
_MAX_BACKOFF_SECONDS = 60


class FetchError(Exception):
    """Permanent: this item is not coming back, whoever asks."""


class Fetcher(Protocol):
    """Contract for fetch workers.

    aclose() releases whatever the implementation holds open between
    fetches.  A connection pool is cheap to rebuild, a browser pool is
    not, so the contract has to allow one.
    """

    async def fetch(self, item: FrontierItem) -> FetchResult: ...

    async def aclose(self) -> None: ...


async def with_retries(
    attempt: Callable[[int], Awaitable[FetchResult]],
    *,
    max_retries: int,
    is_transient: Callable[[BaseException], bool],
    label: str = "",
) -> FetchResult:
    """Run *attempt* until it succeeds, gives up, or fails permanently.

    Which exceptions count as transient is the caller's business, because
    httpx and a browser fail in entirely different vocabularies; the
    backoff schedule is not, so it lives here once. FetchError is always
    permanent and is re-raised untouched.
    """
    last: BaseException | None = None
    for n in range(1, max_retries + 1):
        try:
            return await attempt(n)
        except FetchError:
            raise
        except BaseException as e:
            if not is_transient(e):
                raise
            last = e
            if n >= max_retries:
                break
            delay = min(2**n, _MAX_BACKOFF_SECONDS)
            logger.warning("fetch.retry %s attempt=%d/%d delay=%.1fs error=%s", label, n, max_retries, delay, e)
            await asyncio.sleep(delay)
    raise FetchError(f"fetch failed after {max_retries} attempts") from last
