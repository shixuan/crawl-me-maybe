"""Fetcher contract, errors and shared retry policy."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from crawlme.schemas import FetchResult, FrontierItem

logger = logging.getLogger(__name__)

# Default crawler identity shared by fetchers and robots policy.
DEFAULT_UA = "crawl-me-maybe (research crawler; +https://github.com/crawl-me-maybe)"

# Cap the delay between attempts.
_MAX_BACKOFF_SECONDS = 60


class FetchError(Exception):
    """Fetch failure that the retry helper does not retry."""


class Fetcher(Protocol):
    """Fetch candidates and release persistent resources through aclose()."""

    async def fetch(self, item: FrontierItem) -> FetchResult: ...

    async def aclose(self) -> None: ...


async def with_retries(
    attempt: Callable[[int], Awaitable[FetchResult]],
    *,
    max_retries: int,
    is_transient: Callable[[BaseException], bool],
    label: str = "",
) -> FetchResult:
    """Retry caller-classified transient exceptions with backoff; never retry FetchError.

    max_retries counts total attempts, including the first. Pass the 1-based attempt
    number to the callback; wrap exhausted retries in FetchError with the last cause.
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
