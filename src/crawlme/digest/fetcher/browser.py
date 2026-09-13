"""Fetch rendered pages with optional Playwright storage state.

Playwright is imported lazily. Each fetch uses a fresh page in a shared context."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from crawlme.digest.fetcher.base import DEFAULT_UA, FetchError, with_retries
from crawlme.schemas import URL, FetchResult, FrontierItem, Payload

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Playwright

# What "loaded" means.  networkidle waits for the XHR that a
# JS-built timeline needs; load would return an empty shell.
WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]

logger = logging.getLogger(__name__)

# Deadline for responses triggered by one scroll.
_SCROLL_SETTLE_MS = 6000
# How often to look while waiting.  The wait ends on the answer, so this
# only bounds how long an early one goes unnoticed.
_SCROLL_POLL_MS = 200

_INSTALL_HINT = (
    "playwright is required for --fetcher browser. Install it with:\n"
    "    pip install 'crawl-me-maybe[browser]'\n"
    "    playwright install chromium\n"
    "On Linux the browser also needs system libraries:\n"
    "    playwright install --with-deps chromium"
)


class PlaywrightFetcher:
    """One browser and context per instance, with a fresh page per fetch."""

    def __init__(
        self,
        *,
        storage_state: str | None = None,
        user_agents: list[str] | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        wait_until: WaitUntil = "networkidle",
        headless: bool = True,
        keep_payload: Callable[[str, str], bool] | None = None,
        max_payload_bytes: int = 8 * 1024 * 1024,
        scrolls: int = 0,
    ) -> None:
        self._storage_state = storage_state
        self._uas = user_agents if user_agents else [DEFAULT_UA]
        self._timeout_ms = int(timeout * 1000)
        self._max_retries = max_retries
        self._wait_until = wait_until
        self._headless = headless
        # Retain sub-responses only when the caller supplies a selection predicate.
        self._keep_payload = keep_payload
        self._max_payload_bytes = max_payload_bytes
        # Zero scrolls limits the fetch to the initial page load.
        self._scrolls = scrolls
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        # Serialize page navigation and capture within the shared browser context.
        self._lock = asyncio.Lock()
        # Held only while starting up, so a burst of first fetches
        # produces one browser rather than one each.
        self._start_lock = asyncio.Lock()

    # lifecycle --------------------------------------------------------

    async def _ensure_context(self) -> BrowserContext:
        """Serialize lazy startup so concurrent first fetches cannot leak duplicate browsers."""
        async with self._start_lock:
            if self._context is not None:
                return self._context
            return await self._start_context()

    async def _start_context(self) -> BrowserContext:
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:  # pragma: no cover - depends on install
            raise FetchError(_INSTALL_HINT) from e

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._headless)
        options: dict[str, Any] = {"user_agent": random.choice(self._uas)}  # noqa: S311
        if self._storage_state:
            options["storage_state"] = _load_storage_state(self._storage_state)
        self._context = await self._browser.new_context(**options)
        self._context.set_default_timeout(self._timeout_ms)
        logger.info(
            "browser ready%s",
            " with your session" if self._storage_state else ", not signed in",
        )
        return self._context

    async def aclose(self) -> None:
        """Close the browser context, browser and Playwright driver."""
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    logger.warning("browser.close_failed", exc_info=True)
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                logger.warning("browser.stop_failed", exc_info=True)
        self._context = None
        self._browser = None
        self._pw = None

    # fetch ------------------------------------------------------------

    async def fetch(self, item: FrontierItem) -> FetchResult:
        return await with_retries(
            lambda _n: self._attempt(item),
            max_retries=self._max_retries,
            is_transient=_is_transient,
            label=f"url={item.url.canonical}",
        )

    async def _attempt(self, item: FrontierItem) -> FetchResult:
        started = time.monotonic()
        # Imported here, not at module scope: playwright is optional and
        # the rest of this module keeps it behind TYPE_CHECKING.
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        context = await self._ensure_context()
        payloads: list[Payload] = []
        async with self._lock:
            page = await context.new_page()
            try:
                if self._keep_payload is not None:
                    # Attach listeners before navigation to capture initial content responses.
                    page.on("response", lambda resp: self._collect(resp, payloads))
                try:
                    response = await page.goto(item.url.canonical, wait_until=self._wait_until)
                except PlaywrightTimeout:
                    # Polling pages may never become idle; preserve the rendered DOM on timeout.
                    logger.info("%s was slow to settle, reading what it rendered", item.url.canonical)
                    response = None
                if self._scrolls:
                    await self._scroll_through(page, payloads)
                html = await page.content()
                final_url_str = page.url
            finally:
                await page.close()

        if response is None:
            # Retry only if the timed-out navigation produced no usable content.
            if not html.strip():
                raise FetchError("navigation timed out with an empty document")
            status = 200
        else:
            status = response.status
        if status >= 400:
            # Same split as HttpFetcher: the browser has already followed
            # redirects, so anything left in the 4xx range is permanent.
            raise FetchError(f"Permanent HTTP error: {status}")

        final_url = item.url
        if final_url_str and final_url_str != item.url.canonical:
            final_url = URL(raw=final_url_str, canonical=final_url_str, url_key=final_url_str)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if payloads:
            logger.debug(
                "browser.payloads url=%s kept=%d bytes=%d",
                item.url.canonical,
                len(payloads),
                sum(len(p.body) for p in payloads),
            )
        logger.debug(
            "browser.ok url=%s status=%d bytes=%d duration=%dms",
            item.url.canonical,
            status,
            len(html),
            elapsed_ms,
        )
        headers = dict(response.headers) if response is not None else {}
        return FetchResult(
            item_id=item.item_id,
            url_key=item.url_key,
            url=item.url,
            status_code=status,
            final_url=final_url,
            redirects=[],
            headers=headers,
            content_type=headers.get("content-type", "text/html"),
            raw=html.encode("utf-8", "replace"),
            payloads=payloads,
            fetch_duration_ms=elapsed_ms,
            fetch_attempt=1,
        )

    async def _scroll_through(self, page: Any, payloads: list[Payload]) -> None:
        """Wait for responses triggered by each scroll, up to the deadline.

        Stop on unchanged height only when no payload arrived; virtualized grids can
        load posts without growing."""
        last_height = 0
        for i in range(self._scrolls):
            height = await page.evaluate("document.body.scrollHeight")
            before = len(payloads)
            if height == last_height and i:
                logger.debug("browser.scroll_end url=%s after=%d of %d", page.url, i, self._scrolls)
                return
            last_height = height
            await page.mouse.wheel(0, max(height, 4000))
            await self._wait_for_payload(page, payloads, before)

    async def _wait_for_payload(self, page: Any, payloads: list[Payload], before: int) -> None:
        """Wait for a scroll to be answered, up to the settle deadline."""
        waited = 0
        while waited < _SCROLL_SETTLE_MS:
            await page.wait_for_timeout(_SCROLL_POLL_MS)
            waited += _SCROLL_POLL_MS
            if len(payloads) > before:
                return
        # One scroll going unanswered is not a bad read on its own.
        # Whether the read came out stale is the listing's to say.
        logger.debug("browser.scroll_unanswered url=%s after=%dms", page.url, waited)

    def _collect(self, response: Any, into: list[Payload]) -> None:
        """Keep selected sub-responses within the byte cap; ignore unavailable bodies."""
        keep = self._keep_payload
        if keep is None:
            return
        ctype = ""
        try:
            ctype = (response.headers or {}).get("content-type", "")
            if not keep(response.url, ctype):
                return
        except Exception:
            return
        asyncio.ensure_future(self._read_body(response, ctype, into))  # noqa: RUF006

    async def _read_body(self, response: Any, ctype: str, into: list[Payload]) -> None:
        total = sum(len(p.body) for p in into)
        if total >= self._max_payload_bytes:
            return
        try:
            body = await response.body()
        except Exception:
            logger.debug("browser.payload_gone url=%s", getattr(response, "url", "?"))
            return
        if total + len(body) > self._max_payload_bytes:
            logger.debug("browser.payload_capped url=%s bytes=%d", response.url, total)
            return
        into.append(Payload(url=response.url, content_type=ctype, body=body))


def _load_storage_state(path: str) -> dict[str, Any]:
    """Load saved session state; reject invalid input instead of silently browsing anonymously."""
    p = Path(path)
    if not p.is_file():
        raise FetchError(f"storage state file not found: {path}")
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise FetchError(f"storage state file is not readable JSON: {path}") from e
    if not isinstance(state, dict) or not (state.get("cookies") or state.get("origins")):
        raise FetchError(f"storage state file has no cookies or origins: {path}")
    return state


def _is_transient(err: BaseException) -> bool:
    """Identify navigation failures that can be retried."""
    name = type(err).__name__
    return "Timeout" in name or "TargetClosed" in name or isinstance(err, asyncio.TimeoutError)
