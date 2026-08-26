"""One fetcher that picks another, per candidate.

A crawl reaches more than one kind of page.  Most of the web answers a
plain HTTP request with the page itself; a few platforms answer with a
script that builds it, and to those a plain request gets an empty shell.
Choosing once for the whole run means paying the browser's price on
every ordinary page, or getting shells from the platforms -- and a shell
is the worse half, because nothing errors: the adapter does not claim
it, the page is read as an ordinary one, and the run reports a quiet
week.

The adapters already state which they are (`NEEDS_RENDERING`), and
`claims_url` answers from the address alone, which is what makes the
decision possible before the fetch rather than after it.

Nothing here starts a browser.  The browser fetcher launches on first
use, so a run that never meets a platform never pays for one, and the
pair can be built unconditionally.
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
    """Plain HTTP, except for the addresses a platform has to render.

    Satisfies the same contract as either half, so the scheduler is
    unaware there are two.
    """

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
        # Asked once.  A run either has the install or does not, and
        # answering per fetch would put a filesystem search on the path
        # of every candidate.
        self._can_render = importlib.util.find_spec("playwright") is not None
        self._warned: set[str] = set()

    async def fetch(self, item: FrontierItem) -> FetchResult:
        return await self._pick(item.url.canonical or item.url.raw).fetch(item)

    async def aclose(self) -> None:
        """Both, and in the order they are cheap to lose.

        The browser one is a no-op when nothing ever started it, which
        is the common case for a link-graph crawl.
        """
        await self._http.aclose()
        await self._browser.aclose()

    def _pick(self, url: str) -> Fetcher:
        adapter = self._claimant(url)
        if adapter is None:
            return self._http
        if not self._can_render:
            # Degraded rather than fatal: this is one candidate out of
            # hundreds, and killing the run over it costs more than the
            # page is worth.  Loud, though -- what follows is a page
            # that looks empty for a reason nothing else would state.
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
        """The adapter that says this address is its platform's, if any.

        By address, not by document: the document is what the fetch is
        for.  An adapter that cannot tell from a URL answers no, which
        is why a feed -- recognised by its root element -- correctly
        lands on plain HTTP here.
        """
        for adapter in self._rendered:
            if adapter.claims_url(url):
                return adapter
        return None
