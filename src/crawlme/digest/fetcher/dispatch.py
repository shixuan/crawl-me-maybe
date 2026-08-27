"""One fetcher that picks another, per candidate.

A platform serves a shell to a plain request, and a shell errors on
nothing: the adapter does not claim it and the run reports a quiet
week. Choosing once for the whole run means either that or paying the
browser on every ordinary page.

Nothing here starts a browser; the browser fetcher launches on first
use, so both can be built unconditionally.
"""

from __future__ import annotations

import importlib.util
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crawlme.digest.feed import FeedAdapter
    from crawlme.digest.fetcher.base import Fetcher
    from crawlme.schemas import FetchResult, FrontierItem

logger = logging.getLogger(__name__)


class DispatchingFetcher:
    """Plain HTTP, except for the addresses a platform has to render."""

    def __init__(
        self,
        *,
        http: Fetcher,
        browser: Fetcher,
        adapters: list[FeedAdapter],
    ) -> None:
        self._http = http
        self._browser = browser
        self._rendered = [a for a in adapters if a.NEEDS_RENDERING]
        # Asked once: a filesystem search per candidate buys nothing.
        self._can_render = importlib.util.find_spec("playwright") is not None
        self._warned: set[str] = set()

    async def fetch(self, item: FrontierItem) -> FetchResult:
        return await self._pick(item.url.canonical or item.url.raw).fetch(item)

    async def aclose(self) -> None:
        """Both. The browser one is a no-op when nothing started it."""
        await self._http.aclose()
        await self._browser.aclose()

    def _pick(self, url: str) -> Fetcher:
        adapter = self._claimant(url)
        if adapter is None:
            return self._http
        if not self._can_render:
            # Degraded rather than fatal: one candidate is not worth the
            # run. Loud, though -- what follows looks like an empty page.
            if adapter.PLATFORM not in self._warned:
                self._warned.add(adapter.PLATFORM)
                logger.warning(
                    "fetch.cannot_render platform=%s url=%s "
                    "(playwright is not installed; pages will arrive as shells) "
                    "install:  pip install 'crawl-me-maybe[browser]' && playwright install chromium",
                    adapter.PLATFORM,
                    url,
                )
            return self._http
        return self._browser

    def _claimant(self, url: str) -> FeedAdapter | None:
        """The adapter that claims this address, if any.

        By address, not document: the document is what the fetch is for.
        A feed answers no here and correctly lands on plain HTTP.
        """
        for adapter in self._rendered:
            if adapter.claims_url(url):
                return adapter
        return None
