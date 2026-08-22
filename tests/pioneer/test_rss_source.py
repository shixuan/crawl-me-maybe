from __future__ import annotations

import datetime
import sys
import types
from unittest.mock import patch

import pytest

from crawlme.pioneer.sources.rss import RssSource
from crawlme.schemas import CrawlGoal

_BODY = "Free bubble tea at the Queen Street shop all weekend, first fifty customers only. " * 2


def _entry(**kw):
    """One parsed entry, shaped the way feedparser hands them over."""
    base = {
        "link": "https://example.com/post/1",
        "title": "Weekend giveaway",
        "content": [{"value": f"<p>{_BODY}</p>"}],
        "published_parsed": (2026, 8, 20, 3, 54, 30, 0, 0, 0),
        "author": "/u/someone",
        "tags": [{"term": "deals"}],
    }
    base.update(kw)
    return base


def _feedparser(feeds: dict[str, dict], seen: list | None = None):
    """A stand-in for the library, recording what it was asked for."""

    def parse(url, request_headers=None):
        if seen is not None:
            seen.append((url, request_headers))
        return types.SimpleNamespace(**feeds[url], get=lambda k, d=None: feeds[url].get(k, d))

    return types.SimpleNamespace(parse=parse)


def _install(module):
    return patch.dict(sys.modules, {"feedparser": module})


@pytest.mark.asyncio
async def test_an_entry_arrives_carrying_its_own_text():
    """The funnel used to rank a bare URL while the post sat in the feed."""
    feeds = {"f": {"status": 200, "entries": [_entry()]}}
    with _install(_feedparser(feeds)):
        [c] = await RssSource("f", between=0, backoff=0).discover(CrawlGoal(prompt="p"))
    assert c.text.startswith("Weekend giveaway")
    assert "bubble tea" in c.text
    assert c.posted_at == datetime.datetime(2026, 8, 20, 3, 54, 30, tzinfo=datetime.timezone.utc)
    assert c.signals["author"] == "/u/someone"
    assert c.signals["tags"] == ["deals"]
    assert c.depth == 0


@pytest.mark.asyncio
async def test_a_link_post_keeps_its_title_and_drops_the_boilerplate():
    """ "submitted by /u/name [link] [comments]" is the feed talking, not
    the author: it must not read as a post with a body."""
    feeds = {"f": {"status": 200, "entries": [_entry(content=[{"value": "submitted by /u/x [link] [comments]"}])]}}
    with _install(_feedparser(feeds)):
        [c] = await RssSource("f", between=0, backoff=0).discover(CrawlGoal(prompt="p"))
    assert c.text == "Weekend giveaway"


@pytest.mark.asyncio
async def test_an_entry_without_a_date_says_so():
    """None has to stay distinguishable from a guess: the time window
    filters on this, and an invented date drops real posts silently."""
    feeds = {"f": {"status": 200, "entries": [_entry(published_parsed=None)]}}
    with _install(_feedparser(feeds)):
        [c] = await RssSource("f", between=0, backoff=0).discover(CrawlGoal(prompt="p"))
    assert c.posted_at is None


@pytest.mark.asyncio
async def test_the_crawls_own_user_agent_is_sent():
    """The library's default announces feedparser, and the same feed
    answered 429 to that name and 200 to this project's."""
    seen: list = []
    feeds = {"f": {"status": 200, "entries": [_entry()]}}
    with _install(_feedparser(feeds, seen)):
        await RssSource("f", user_agent="crawlme/1.0", between=0, backoff=0).discover(CrawlGoal(prompt="p"))
    assert seen == [("f", {"User-Agent": "crawlme/1.0"})]


@pytest.mark.asyncio
async def test_several_feeds_are_read_and_deduplicated():
    """Watching a platform means watching a handful of its corners, and
    the same post turns up in two of them."""
    shared = _entry(link="https://example.com/shared")
    feeds = {
        "a": {"status": 200, "entries": [_entry(link="https://example.com/1"), shared]},
        "b": {"status": 200, "entries": [shared, _entry(link="https://example.com/2")]},
    }
    with _install(_feedparser(feeds)):
        out = await RssSource(["a", "b"], between=0, backoff=0).discover(CrawlGoal(prompt="p"))
    assert [c.url.raw for c in out] == [
        "https://example.com/1",
        "https://example.com/shared",
        "https://example.com/2",
    ]


@pytest.mark.asyncio
async def test_a_refused_feed_costs_its_own_entries_only():
    """One subreddit rate-limiting must not empty the whole run."""
    feeds = {
        "bad": {"status": 429, "entries": [_entry(link="https://example.com/never")]},
        "good": {"status": 200, "entries": [_entry(link="https://example.com/ok")]},
    }
    with _install(_feedparser(feeds)):
        out = await RssSource(["bad", "good"], between=0, backoff=0).discover(CrawlGoal(prompt="p"))
    assert [c.url.raw for c in out] == ["https://example.com/ok"]


@pytest.mark.asyncio
async def test_a_feed_that_raises_costs_its_own_entries_only():
    def parse(url, request_headers=None):
        if url == "boom":
            raise OSError("network gone")
        return types.SimpleNamespace(entries=[_entry(link="https://example.com/ok")], get=lambda k, d=None: 200)

    with _install(types.SimpleNamespace(parse=parse)):
        out = await RssSource(["boom", "fine"], between=0, backoff=0).discover(CrawlGoal(prompt="p"))
    assert [c.url.raw for c in out] == ["https://example.com/ok"]


@pytest.mark.asyncio
async def test_a_rate_limited_feed_gets_one_second_chance():
    """Two of a platform's feeds read back to back answered 200 then 429.

    Seed discovery does not go through the fetcher, so none of the
    crawl's politeness reaches it; without a retry, watching twenty
    subreddits would lose most of them on the first pass.
    """
    calls: list = []

    def parse(url, request_headers=None):
        calls.append(url)
        status = 429 if len(calls) == 1 else 200
        entries = [] if status == 429 else [_entry(link="https://example.com/ok")]
        return types.SimpleNamespace(entries=entries, get=lambda k, d=None: status if k == "status" else d)

    with _install(types.SimpleNamespace(parse=parse)):
        out = await RssSource("f", between=0, backoff=0).discover(CrawlGoal(prompt="p"))
    assert calls == ["f", "f"], "one retry, not a loop"
    assert [c.url.raw for c in out] == ["https://example.com/ok"]


@pytest.mark.asyncio
async def test_feeds_are_paced_apart():
    """The gap is the only politeness this path has."""
    slept: list = []

    async def fake_sleep(n):
        slept.append(n)

    feeds = {u: {"status": 200, "entries": [_entry(link=f"https://example.com/{u}")]} for u in ("a", "b", "c")}
    with _install(_feedparser(feeds)):
        with patch("crawlme.pioneer.sources.rss.asyncio.sleep", fake_sleep):
            await RssSource(["a", "b", "c"], between=2.0).discover(CrawlGoal(prompt="p"))
    assert slept == [2.0, 2.0], "between feeds, not before the first"
