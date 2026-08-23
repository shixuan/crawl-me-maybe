"""Fetch pages through a real browser, with an optional logged-in session.

Same Fetcher contract as HttpFetcher, so the engine cannot tell them
apart.  Two things justify the cost of a browser:

  - the page builds its content with JavaScript, so the HTML that arrives
    over HTTP is an empty shell
  - the platform requires a session, and hands anonymous requests a login
    wall instead of the content

The session comes from a storage_state JSON file the user exports
themselves (`playwright codegen`, or a browser extension).  This module
never sees a password and never logs anything in.

playwright is an optional dependency and is imported lazily, so nothing
here costs anything until a run actually asks for a browser:

    pip install 'crawl-me-maybe[browser]'
    playwright install chromium
"""

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

#: What "loaded" means.  networkidle waits for the XHR that a
#: JS-built timeline needs; load would return an empty shell.
WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]

logger = logging.getLogger(__name__)

#: Time for a lazily-built page to answer one scroll.  Long enough for a
#: request to come back on a slow connection, short enough that a page
#: with nothing left costs little.
_SCROLL_SETTLE_MS = 1500

_INSTALL_HINT = (
    "playwright is required for --fetcher browser. Install it with:\n"
    "    pip install 'crawl-me-maybe[browser]'\n"
    "    playwright install chromium\n"
    "On Linux the browser also needs system libraries:\n"
    "    playwright install --with-deps chromium"
)


class PlaywrightFetcher:
    """One browser per run, one fresh page per fetch.

    The browser and the logged-in context are expensive to build and are
    reused; a page is cheap and is discarded after every fetch so one
    page's state cannot leak into the next.
    """

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
        # What a page fetches for itself is dropped unless something asks
        # for it, so a crawl that has no use for it pays nothing at all.
        # The fetcher cannot know which response matters; whoever does
        # passes the predicate in.
        self._keep_payload = keep_payload
        self._max_payload_bytes = max_payload_bytes
        # How many times to ask a lazily-built page for more of itself.
        # Zero keeps the old behaviour: one screen, one set of requests.
        # Scrolling is how a reader reaches the rest, and it makes the
        # page issue the same requests it made for the first screen, so
        # nothing here forges anything the page would not send itself.
        self._scrolls = scrolls
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        # One page at a time per browser: a shared context is not safe to
        # drive concurrently, and fetch_concurrency already bounds the
        # callers.  This keeps the browser honest about that.
        self._lock = asyncio.Lock()
        # Held only while starting up, so a burst of first fetches
        # produces one browser rather than one each.
        self._start_lock = asyncio.Lock()

    #: lifecycle --------------------------------------------------------

    async def _ensure_context(self) -> BrowserContext:
        """Start the browser on first use and reuse it afterwards.

        Lazy because constructing a scheduler must not launch a browser,
        and because the import itself is optional.

        Guarded because the first fetches arrive together: the pump pops
        several seeds at once, every one of them finds no context, and
        every one of them launches a browser.  The last assignment wins
        and the rest become processes nobody holds a reference to, so
        aclose() cannot reach them.  One run showed five starts where it
        should have shown one.
        """
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
            "browser.started headless=%s session=%s",
            self._headless,
            "yes" if self._storage_state else "anonymous",
        )
        return self._context

    async def aclose(self) -> None:
        """Tear the browser down.  Safe to call more than once.

        A browser that outlives the run keeps a process tree alive, which
        is the same class of leak the aiosqlite worker thread once was.
        """
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

    #: fetch ------------------------------------------------------------

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
                    # Attached before navigating: a listener added after
                    # would miss the requests that fill the first screen,
                    # which are exactly the ones carrying the content.
                    page.on("response", lambda resp: self._collect(resp, payloads))
                try:
                    response = await page.goto(item.url.canonical, wait_until=self._wait_until)
                except PlaywrightTimeout:
                    # "networkidle" is a condition some pages never
                    # reach: a platform that polls, streams, or throttles
                    # keeps a request open forever.  Waiting it out still
                    # rendered the page and still collected the payloads,
                    # so the timeout says the condition failed, not that
                    # the fetch did.  Take what is there and let the
                    # downstream emptiness check decide -- discarding it
                    # here threw away a page we had already paid for, and
                    # then paid for it twice more on retry.
                    logger.info("browser.wait_timeout url=%s taking what rendered", item.url.canonical)
                    response = None
                if self._scrolls:
                    await self._scroll_through(page)
                html = await page.content()
                final_url_str = page.url
            finally:
                await page.close()

        if response is None:
            # The wait condition timed out.  If something rendered, that
            # is the page and the condition was simply unreachable; if
            # nothing did, the fetch really failed and should retry.
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

    async def _scroll_through(self, page: Any) -> None:
        """Ask the page for more of itself, and stop when it stops giving.

        A listing hands out one screen at a time, so a window measured in
        weeks is answered with the dozen most recent items unless someone
        keeps asking. The height check is what makes it stop early on a
        short account rather than spend every scroll on a page that has
        already ended.
        """
        last_height = 0
        for i in range(self._scrolls):
            height = await page.evaluate("document.body.scrollHeight")
            if height == last_height and i:
                logger.debug("browser.scroll_end url=%s after=%d", page.url, i)
                return
            last_height = height
            await page.mouse.wheel(0, max(height, 4000))
            await page.wait_for_timeout(_SCROLL_SETTLE_MS)

    def _collect(self, response: Any, into: list[Payload]) -> None:
        """Keep one response the page asked for, if anyone wants it.

        Fire-and-forget: the listener is sync, reading a body is not, and
        a body can be gone by the time it is asked for. A payload that
        does not arrive is a weaker crawl, never a failed one, so every
        failure here is swallowed after a debug line.
        """
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
            logger.info("browser.payload_capped url=%s bytes=%d", response.url, total)
            return
        into.append(Payload(url=response.url, content_type=ctype, body=body))


def _load_storage_state(path: str) -> dict[str, Any]:
    """Read an exported session, failing loudly rather than anonymously.

    A missing or malformed session file would otherwise degrade into an
    anonymous browser, which on a login-walled platform means crawling a
    login page a few hundred times and concluding the site is empty.
    """
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
    """A navigation that timed out or was interrupted is worth retrying.

    Rendering fails transiently more often than an HTTP GET does, so the
    browser needs this at least as much as httpx: a slow page, a resource
    that never settles, a renderer that died mid-navigation.
    """
    name = type(err).__name__
    return "Timeout" in name or "TargetClosed" in name or isinstance(err, asyncio.TimeoutError)
