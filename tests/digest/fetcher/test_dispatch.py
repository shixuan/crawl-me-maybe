"""Which fetcher a candidate lands on, and what happens when the one it
needs is not installed."""

from __future__ import annotations

import logging

import pytest

from crawlme.digest.feed import ADAPTERS, instagram, reddit, rss
from crawlme.digest.fetcher import DispatchingFetcher
from crawlme.schemas import URL, FetchResult, FrontierItem


class _Recording:
    """Stands in for a real fetcher and remembers what it was asked for."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.urls: list[str] = []
        self.closed = False

    async def fetch(self, item: FrontierItem) -> FetchResult:
        self.urls.append(item.url.canonical)
        return FetchResult(item_id=item.item_id, url=item.url, url_key=item.url_key, status=200)

    async def aclose(self) -> None:
        self.closed = True


def _item(url: str) -> FrontierItem:
    return FrontierItem(
        url_key=url,
        url=URL(raw=url, canonical=url, url_key=url, reg_domain=""),
    )


def _pair(adapters=ADAPTERS, *, can_render: bool = True):
    http, browser = _Recording("http"), _Recording("browser")
    d = DispatchingFetcher(http=http, browser=browser, adapters=list(adapters))
    d._can_render = can_render
    return d, http, browser


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.reddit.com/r/Python/", "browser"),
        ("https://www.reddit.com/r/Python/comments/abc/slug/", "browser"),
        ("https://www.instagram.com/someone/", "browser"),
        ("https://blog.rust-lang.org/feed.xml", "http"),
        ("https://example.com/some/article", "http"),
        ("https://news.ycombinator.com", "http"),
    ],
)
async def test_a_candidate_goes_to_the_fetcher_its_platform_needs(url, expected):
    """Decided from the address, because the document is what the fetch
    is for.  A feed is recognised by its root element and so answers no
    to claims_url -- which is why it correctly lands on plain HTTP."""
    d, http, browser = _pair()
    await d.fetch(_item(url))
    assert (browser.urls if expected == "browser" else http.urls) == [url]
    assert (http.urls if expected == "browser" else browser.urls) == []


async def test_the_ordinary_web_never_starts_a_browser():
    """The point of dispatching: a link-graph crawl pays nothing."""
    d, http, browser = _pair()
    for url in ("https://a.com/", "https://b.org/x", "https://c.net/y?z=1"):
        await d.fetch(_item(url))
    assert len(http.urls) == 3
    assert browser.urls == []


async def test_an_adapter_that_does_not_need_rendering_is_not_consulted():
    """RSS never claims a URL, so a run with only RSS enabled has
    nothing that could route to a browser."""
    d, http, browser = _pair([rss])
    await d.fetch(_item("https://www.reddit.com/r/Python/"))
    assert http.urls == ["https://www.reddit.com/r/Python/"]
    assert browser.urls == []


async def test_without_playwright_the_page_still_gets_fetched(caplog):
    """One candidate out of hundreds.  Refusing the run over it costs
    more than the page is worth, but silence would leave a page that
    looks empty for a reason nothing else states."""
    d, http, browser = _pair(can_render=False)
    with caplog.at_level(logging.WARNING):
        await d.fetch(_item("https://www.reddit.com/r/Python/"))
    assert http.urls == ["https://www.reddit.com/r/Python/"]
    assert browser.urls == []
    assert any("fetch.cannot_render" in r.message for r in caplog.records)


async def test_the_warning_is_said_once_per_platform(caplog):
    """A subreddit yields fifty links; fifty identical warnings would
    bury whatever else the run had to say."""
    d, _, _ = _pair(can_render=False)
    with caplog.at_level(logging.WARNING):
        for i in range(5):
            await d.fetch(_item(f"https://www.reddit.com/r/Python/comments/{i}/x/"))
    assert sum("fetch.cannot_render" in r.message for r in caplog.records) == 1


async def test_closing_closes_both():
    """The browser one is a no-op when nothing started it, which is the
    common case -- but a run that did start one must not leave the
    process tree behind."""
    d, http, browser = _pair()
    await d.aclose()
    assert http.closed and browser.closed


def test_every_rendered_adapter_can_answer_from_a_url():
    """A platform that needs rendering but cannot recognise its own
    addresses would be routed to plain HTTP forever, and the shell it
    got back would read as an empty week."""
    for adapter in (a for a in ADAPTERS if a.NEEDS_RENDERING):
        assert adapter.claims_url(f"https://www.{adapter.DOMAIN}/anything")


def test_the_two_platforms_do_not_claim_each_other():
    """Both need a browser, so a mix-up would not show up as a failed
    fetch -- it would show up as the wrong parser finding nothing."""
    assert not reddit.claims_url("https://www.instagram.com/someone/")
    assert not instagram.claims_url("https://www.reddit.com/r/Python/")


def test_a_walled_platform_without_a_session_is_not_routed_to_a_browser():
    """Spending one there buys a login page.  The adapter is already
    left out of a session-less run, so the dispatcher never hears about
    the platform and the address travels as any other would.

    Read through adapters_for rather than ADAPTERS: the list a run
    actually gets is what decides this, and it is not the whole set.
    """
    from crawlme.config import Settings
    from crawlme.scheduler.factory import adapters_for

    d, http, browser = _pair(adapters_for(Settings(_env_file=None)))
    assert not any(a.NEEDS_SESSION for a in d._rendered)
    assert d._pick("https://www.instagram.com/someone/") is http
    assert d._pick("https://www.reddit.com/r/Python/") is browser
