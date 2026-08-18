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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from crawlme.digest.fetcher.base import DEFAULT_UA, FetchError, with_retries
from crawlme.schemas import URL, FetchResult, FrontierItem

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Playwright

#: What "loaded" means.  networkidle waits for the XHR that a
#: JS-built timeline needs; load would return an empty shell.
WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]

logger = logging.getLogger(__name__)

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
    ) -> None:
        self._storage_state = storage_state
        self._uas = user_agents if user_agents else [DEFAULT_UA]
        self._timeout_ms = int(timeout * 1000)
        self._max_retries = max_retries
        self._wait_until = wait_until
        self._headless = headless
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        # One page at a time per browser: a shared context is not safe to
        # drive concurrently, and fetch_concurrency already bounds the
        # callers.  This keeps the browser honest about that.
        self._lock = asyncio.Lock()

    #: lifecycle --------------------------------------------------------

    async def _ensure_context(self) -> BrowserContext:
        """Start the browser on first use and reuse it afterwards.

        Lazy because constructing a scheduler must not launch a browser,
        and because the import itself is optional.
        """
        if self._context is not None:
            return self._context
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
        context = await self._ensure_context()
        async with self._lock:
            page = await context.new_page()
            try:
                response = await page.goto(item.url.canonical, wait_until=self._wait_until)
                html = await page.content()
                final_url_str = page.url
            finally:
                await page.close()

        status = response.status if response is not None else 0
        if status >= 400:
            # Same split as HttpFetcher: the browser has already followed
            # redirects, so anything left in the 4xx range is permanent.
            raise FetchError(f"Permanent HTTP error: {status}")

        final_url = item.url
        if final_url_str and final_url_str != item.url.canonical:
            final_url = URL(raw=final_url_str, canonical=final_url_str, url_key=final_url_str)

        elapsed_ms = int((time.monotonic() - started) * 1000)
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
            fetch_duration_ms=elapsed_ms,
            fetch_attempt=1,
        )


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
