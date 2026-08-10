"""HTTP fetch worker.

Takes a FrontierItem (essentially a URL) and downloads the page.  This is the
only module in the system that touches the network, and it deliberately knows
nothing about crawling strategy, ranking, or content analysis.

Error handling
--------------
Responses are classified into three buckets:

    Transient (retryable)
        5xx server errors, connection timeouts, DNS failures, and 429 rate
        limits.  Exponential backoff: 2^attempt seconds, capped at 60s.
        For 429 specifically, the Retry-After header is respected before
        retrying.

    Permanent (fatal)
        4xx client errors (except 429).  These raise FetchError immediately
        without retrying — the page doesn't exist, we're forbidden, etc.

    Success (2xx, 3xx)
        Returned normally.  3xx redirects are followed manually (not via
        httpx's follow_redirects) so we can record the full redirect chain
        in FetchResult.redirects.

_TransientError is an internal signal used in _do_fetch to tell the outer
fetch() retry loop to back off and try again.  It never escapes the module.

Redirect handling
-----------------
httpx's built-in follow_redirects discards the intermediate hops.  We need
the full chain for canonicalization and link-graph tracking, so we follow
manually: loop on 3xx, resolve Location relative to the current URL, append
to redirects, and repeat until we land on a non-3xx response.

User-Agent rotation
-------------------
A UA is picked at random from the configured pool for each request to reduce
the chance of being fingerprint-blocked.  The default pool is a single
Chrome/Win UA; pass a longer list for production use."""

from __future__ import annotations

import asyncio
import datetime
import random
import time
from urllib.parse import urljoin

import httpx

from crawlme.schemas import URL, FetchResult, FrontierItem


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


_DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"


class FetchError(Exception):
    pass


class Fetcher:
    def __init__(
        self,
        user_agents: list[str] | None = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._uas = user_agents if user_agents else [_DEFAULT_UA]
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._max_retries = max_retries

    async def fetch(self, item: FrontierItem) -> FetchResult:
        last_err: BaseException | None = None

        for attempt in range(1, self._max_retries + 1):
            started = time.monotonic()
            try:
                result = await self._do_fetch(item, attempt, started)
                return result
            except _TransientError as e:
                last_err = e.__cause__ or e
                if attempt < self._max_retries:
                    delay = min(2**attempt, 60)
                    await asyncio.sleep(delay)
            except FetchError:
                raise

        raise FetchError(f"Fetch failed after {self._max_retries} attempts") from last_err

    async def _do_fetch(self, item: FrontierItem, attempt: int, started: float) -> FetchResult:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._connect_timeout, read=self._read_timeout),
            follow_redirects=False,
            headers={"User-Agent": random.choice(self._uas)},  # noqa: S311
        ) as client:
            response = await client.get(item.url.raw)

            redirects: list[URL] = []
            final_url_str = item.url.raw
            final_url_obj = item.url

            while response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                if not location:
                    break
                final_url_str = urljoin(final_url_str, location)
                final_url_obj = URL(raw=final_url_str, canonical=final_url_str, url_key=final_url_str)
                redirects.append(final_url_obj)
                response = await client.get(final_url_str)

            # 429: rate-limited — wait Retry-After seconds, then retry.
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "5")
                try:
                    delay = int(retry_after)
                except ValueError:
                    delay = 5
                await asyncio.sleep(delay)
                raise _TransientError("429 Too Many Requests")

            # 5xx: transient server error — retry.
            if response.status_code >= 500:
                raise _TransientError(f"Server error {response.status_code}")

            # 4xx (non-429): permanent — do not retry.
            if 400 <= response.status_code < 500:
                raise FetchError(f"Permanent HTTP error: {response.status_code}")

            elapsed_ms = int((time.monotonic() - started) * 1000)

            return FetchResult(
                item_id=item.item_id,
                url_key=item.url_key,
                url=item.url,
                status_code=response.status_code,
                final_url=final_url_obj,
                redirects=redirects,
                headers=dict(response.headers),
                content_type=response.headers.get("Content-Type", ""),
                raw=response.content,
                fetch_duration_ms=elapsed_ms,
                fetched_at=_utcnow(),
                fetch_attempt=attempt,
            )


class _TransientError(Exception):
    """Internal signal that a retryable error occurred."""
