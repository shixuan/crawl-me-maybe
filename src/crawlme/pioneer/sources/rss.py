"""RSS/Atom source: seeds that arrive carrying their own content.

A feed entry is not a bare link.  It states a title, a publication time,
often an author, and frequently the whole post.  Reading only the href
threw all of that away and left the funnel ranking a URL, which is the
same blind guessing a link graph is stuck with -- except here the text
was already in hand.

That matters most where a platform is otherwise expensive: a subreddit's
feed hands over the post bodies, at a median of nine hundred characters,
without a browser, a login, or a second request.

Needs feedparser, an optional extra: pip install 'crawl-me-maybe[rss]'.
"""

from __future__ import annotations

import asyncio
import datetime
import html
import logging
import re
from typing import Any

from crawlme.pioneer.sources.base import _make_candidate
from crawlme.schemas import Candidate, CrawlGoal

logger = logging.getLogger(__name__)

#: Entries whose body is only the feed's own boilerplate carry no text
#: worth ranking: a link post says "submitted by /u/name [link]" and
#: keeps its content at the far end of the link.
_MIN_BODY_CHARS = 80

_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")

#: Seconds between feeds.  Seed discovery does not go through the
#: fetcher, so none of the crawl's politeness reaches it, and platforms
#: limit feeds tightly: read back to back, one platform answered 200 and
#: then 429.  Measured at two seconds it lost a third of a four-feed
#: pass; at ten it lost a quarter.
_BETWEEN_FEEDS = 10.0

#: How long to wait out a refusal before the one retry.  A limiter that
#: says "too many" is worth believing once; twice is a platform saying
#: no, and a run should not spend its start-up arguing.
#:
#: No pair of numbers makes this reliable.  The same pass that recovered
#: one refused feed after thirty seconds was refused twice on another,
#: so the limiter is not a fixed window and cannot be paced around from
#: one address.  Losing a feed is therefore normal, which is why one
#: costs its own entries and never the run.
_BACKOFF = 30.0


class RssSource:
    """Feeds, read once each, with whatever their entries already say.

    Several feeds rather than one because watching a platform means
    watching a handful of its corners -- twenty subreddits is twenty
    feeds -- and a composite source that only ever composes this one
    would be a layer with nothing else to hold.

    *user_agent* is the crawl's own.  Left to the library's default it
    announces feedparser, which some platforms rate-limit on sight: the
    same feed answered 429 to feedparser's name and 200 to this
    project's.
    """

    def __init__(
        self,
        url: str | list[str],
        user_agent: str = "",
        between: float = _BETWEEN_FEEDS,
        backoff: float = _BACKOFF,
    ) -> None:
        self._urls = [url] if isinstance(url, str) else list(url)
        self._user_agent = user_agent
        self._between = between
        self._backoff = backoff

    async def discover(self, goal: CrawlGoal) -> list[Candidate]:
        try:
            import feedparser
        except ImportError:
            raise ImportError("reading feeds needs feedparser: pip install 'crawl-me-maybe[rss]'") from None

        headers = {"User-Agent": self._user_agent} if self._user_agent else None
        out: list[Candidate] = []
        seen: set[str] = set()
        for i, url in enumerate(self._urls):
            if i:
                await asyncio.sleep(self._between)
            for c in await self._read_with_retry(feedparser, url, headers):
                # The same post can appear in two feeds a run watches;
                # the frontier would dedup it later, but not before it
                # had been ranked twice.
                if c.url.raw in seen:
                    continue
                seen.add(c.url.raw)
                out.append(c)
        return out

    async def _read_with_retry(self, feedparser: Any, url: str, headers: dict[str, str] | None) -> list[Candidate]:
        """One feed, with one second chance if the platform said wait."""
        out, status = self._read(feedparser, url, headers)
        if status != 429:
            return out
        logger.info("rss.backoff url=%s seconds=%.0f", url, self._backoff)
        await asyncio.sleep(self._backoff)
        return self._read(feedparser, url, headers)[0]

    def _read(self, feedparser: Any, url: str, headers: dict[str, str] | None) -> tuple[list[Candidate], int]:
        """One feed and the status it answered with.

        A feed that fails costs its own entries, not the run: watching a
        platform means watching a handful of its corners, and one of them
        rate-limiting must not empty the other nineteen.
        """
        try:
            feed = feedparser.parse(url, request_headers=headers)
        except Exception as e:  # feedparser raises little, the network raises plenty
            logger.warning("rss.unreadable url=%s error=%s", url, e)
            return [], 0
        status = int(feed.get("status") or 0)
        if status >= 400:
            # Loud, because the alternative is a run that starts, finds
            # nothing, and reports itself finished.
            logger.warning("rss.refused url=%s status=%s", url, status)
            return [], status
        out = [_from_entry(e, e["link"]) for e in feed.entries if e.get("link")]
        logger.info("rss.read url=%s entries=%d with_text=%d", url, len(out), sum(1 for c in out if c.text))
        return out, status


def _from_entry(entry: Any, link: str) -> Candidate:
    """Carry across everything the entry states, and nothing it does not."""
    c = _make_candidate(link)
    title = str(entry.get("title", "")).strip()
    body = _body(entry)
    # The title is part of the post, not decoration around it: on a link
    # post it is the whole of what the author wrote.
    c.text = f"{title}\n\n{body}".strip() if body else title
    c.anchor = title or None
    c.posted_at = _published(entry)
    signals: dict[str, Any] = {"feed_title": title}
    author = str(entry.get("author", "")).strip()
    if author:
        signals["author"] = author
    tags = [t.get("term", "") for t in entry.get("tags", []) if t.get("term")]
    if tags:
        signals["tags"] = tags
    c.signals = signals
    return c


def _body(entry: Any) -> str:
    """The entry's own prose, stripped of the markup feeds wrap it in."""
    raw = ""
    if entry.get("content"):
        raw = entry["content"][0].get("value", "")
    elif entry.get("summary"):
        raw = entry["summary"]
    text = _SPACE.sub(" ", _TAGS.sub(" ", html.unescape(raw))).strip()
    return text if len(text) >= _MIN_BODY_CHARS else ""


def _published(entry: Any) -> datetime.datetime | None:
    """When the entry says it was published, in UTC, or None.

    None is the ordinary answer for a feed that omits the field, and it
    has to stay distinguishable from a guess: the time window filters on
    this, and an invented date would drop real posts silently.
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        y, mo, d, h, mi, sec = (int(x) for x in parsed[:6])
    except (TypeError, ValueError):
        return None
    try:
        return datetime.datetime(y, mo, d, h, mi, sec, tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
