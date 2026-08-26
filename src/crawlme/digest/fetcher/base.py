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

# How the crawler names itself when nothing else was configured.  It
# says what it is and where to find whoever ran it, which is the only
# thing a User-Agent is good for from the far end.  Claiming to be a
# browser instead would contradict the rest of the project, and the
# platforms that turn crawlers away are not fooled by the string alone.
#
# No version: nothing in the code reads one, so it would be a number
# kept in step by hand with the one in pyproject, and it was already
# four releases behind.  The address is the part that does the work.
#
# The settings default reads this, so the name is stated once here.
DEFAULT_UA = "crawl-me-maybe (research crawler; +https://github.com/crawl-me-maybe)"

# Backoff is capped so a long retry cannot outlive the crawl itself.
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
