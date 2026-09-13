"""Fetch HTTP pages with redirect tracking, retries and a total deadline.

Redirects are followed explicitly to retain every hop. The total deadline also
bounds responses that continuously reset the per-read timeout."""

from __future__ import annotations

import asyncio
import datetime
import logging
import random
import time
from urllib.parse import urljoin

import httpx

from crawlme.digest.fetcher.base import DEFAULT_UA, FetchError, with_retries
from crawlme.schemas import URL, FetchResult, FrontierItem

logger = logging.getLogger(__name__)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# Hard cap on the manual redirect chain; loops are detected separately.
_MAX_REDIRECTS = 10


class HttpFetcher:
    def __init__(
        self,
        user_agents: list[str] | None = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        max_retries: int = 3,
        total_timeout: float | None = None,
    ) -> None:
        self._uas = user_agents if user_agents else [DEFAULT_UA]
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._max_retries = max_retries
        # Bound trickling responses that never trigger the per-read timeout.
        self._total_timeout = total_timeout if total_timeout is not None else connect_timeout + read_timeout + 10.0
        # Create the shared connection pool lazily on the running event loop.
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._connect_timeout, read=self._read_timeout),
                follow_redirects=False,
            )
        return self._client

    async def aclose(self) -> None:
        """Close the shared client.  Safe to call more than once."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, item: FrontierItem) -> FetchResult:
        async def attempt(n: int) -> FetchResult:
            return await asyncio.wait_for(
                self._do_fetch(item, n, time.monotonic()),
                timeout=self._total_timeout,
            )

        return await with_retries(
            attempt,
            max_retries=self._max_retries,
            is_transient=_is_transient,
            label=f"url={item.url.canonical}",
        )

    async def _do_fetch(self, item: FrontierItem, attempt: int, started: float) -> FetchResult:
        client = self._get_client()
        # Rotate the User-Agent per request while reusing the client.
        headers = {"User-Agent": random.choice(self._uas)}  # noqa: S311
        response = await client.get(item.url.canonical, headers=headers)

        redirects: list[URL] = []
        final_url_str = item.url.canonical
        final_url_obj = item.url
        seen: set[str] = {final_url_str}

        while response.status_code in (301, 302, 303, 307, 308):
            if len(redirects) >= _MAX_REDIRECTS:
                raise FetchError(f"too many redirects (>{_MAX_REDIRECTS})")
            location = response.headers.get("Location", "")
            if not location:
                break
            final_url_str = urljoin(final_url_str, location)
            if final_url_str in seen:
                raise FetchError(f"redirect loop detected at {final_url_str}")
            seen.add(final_url_str)
            final_url_obj = URL(raw=final_url_str, canonical=final_url_str, url_key=final_url_str)
            redirects.append(final_url_obj)
            response = await client.get(final_url_str, headers=headers)

        # 429: rate-limited: wait Retry-After seconds, then retry.
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "5")
            try:
                delay = int(retry_after)
            except ValueError:
                delay = 5
            await asyncio.sleep(delay)
            raise _TransientError("429 Too Many Requests")

        # 5xx: transient server error: retry.
        if response.status_code >= 500:
            logger.warning("fetch.5xx url=%s status=%d", item.url.canonical, response.status_code)
            raise _TransientError(f"Server error {response.status_code}")

        # 4xx (non-429): permanent: do not retry.
        if 400 <= response.status_code < 500:
            logger.warning("fetch.4xx url=%s status=%d", item.url.canonical, response.status_code)
            raise FetchError(f"Permanent HTTP error: {response.status_code}")

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.debug(
            "fetch.ok url=%s status=%d bytes=%d duration=%dms",
            item.url.canonical,
            response.status_code,
            len(response.content),
            elapsed_ms,
        )

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


def _is_transient(err: BaseException) -> bool:
    """A total-deadline timeout and a 5xx/429 both deserve another try."""
    return isinstance(err, asyncio.TimeoutError | _TransientError)
