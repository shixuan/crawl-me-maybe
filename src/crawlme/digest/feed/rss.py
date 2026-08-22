"""RSS/Atom: the one feed whose shape is a format, not a platform.

Every other adapter here knows one site's markup.  This one knows a
document type that any site can serve, so it claims pages by what the
document is rather than by where it came from.  That is also why it
cannot claim a URL: measured against seven real feeds, only one ended in
``.rss`` -- the rest were ``/feed``, ``/rss``, ``feed.xml``, ``atom.xml``,
``/feed/rss/``.  Content type is no better, arriving as four different
strings.  The root element is the only reliable signal, and it is only
readable once the document is in hand.

A feed inverts the shape the other adapters have.  A listing there is
weak and its items are strong; here the listing carries the posts
themselves -- title, publication time, author, and for a self post the
whole body -- so what it yields is already worth ranking before anything
else is fetched.

Needs feedparser, an optional extra: pip install 'crawl-me-maybe[rss]'.
"""

from __future__ import annotations

import datetime
import html as html_mod
import logging
import re
from typing import Any

from crawlme.digest.feed.base import FeedItem, Listing, PageProblem
from crawlme.schemas import Page, Payload

logger = logging.getLogger(__name__)

PLATFORM = "rss"

#: No single host serves this: any site can. Kept because the contract
#: asks for it, and empty is the honest answer.
DOMAIN = ""

#: A feed is served to anyone; nothing here is behind a login.
NEEDS_SESSION = False

#: The document's root, which is the only reliable way to know one.
#: Looked for near the top so a mention of the word later in a page
#: cannot make an HTML document read as a feed.
_ROOT = re.compile(r"<\s*(rss|feed|rdf:RDF)\b", re.I)
_HEAD_CHARS = 2000

#: Bodies shorter than this are the feed's own boilerplate rather than
#: the author's words: a link post reads "submitted by /u/name [link]
#: [comments]" and keeps its content at the far end of the link.
_MIN_BODY_CHARS = 80

_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def claims_url(url: str) -> bool:
    """Never: an address does not say whether it serves a feed.

    Answering False leaves this adapter out of every check that runs
    before a fetch, which is correct rather than merely safe.  Guessing
    from a suffix would be wrong five times in seven.
    """
    return False


def claims(page: Page, document: str) -> bool:
    """Ours if the document says it is one."""
    return _ROOT.search(document[:_HEAD_CHARS]) is not None


def problem(html: str) -> PageProblem | None:
    """A feed that parses is content; one that does not is not ours.

    Nothing here maps onto the walls the other adapters report: a feed
    is served to strangers, so there is no login page to mistake for an
    empty week.
    """
    return None


def keeps_payload(url: str, content_type: str) -> bool:
    """Nothing: a feed states its posts in the document it hands over."""
    return False


def parse_item(html: str, url: str = "") -> FeedItem | None:
    """None: a feed document is a listing, never a single post."""
    return None


def parse_listing(html: str, url: str, payloads: list[Payload]) -> Listing:
    """Read the feed into its entries, each with what it already says."""
    try:
        import feedparser
    except ImportError:  # pragma: no cover - depends on the install
        logger.warning("rss.no_feedparser url=%s", url)
        return Listing()

    feed = feedparser.parse(html)
    items: list[FeedItem] = []
    for entry in feed.entries:
        link = str(entry.get("link", "")).strip()
        if not link:
            continue
        items.append(_from_entry(entry, link))
    logger.info("rss.parsed url=%s entries=%d with_text=%d", url, len(items), sum(1 for i in items if i.text))
    # Every entry is the feed's own: a feed does not carry other
    # people's posts the way a platform's grid carries tagged ones.
    return Listing(own=items)


def _from_entry(entry: Any, link: str) -> FeedItem:
    title = str(entry.get("title", "")).strip()
    body = _body(entry)
    signals: dict[str, Any] = {}
    if title:
        signals["title"] = title
    tags = [t.get("term", "") for t in entry.get("tags", []) if t.get("term")]
    if tags:
        signals["tags"] = tags
    return FeedItem(
        permalink=link,
        platform=PLATFORM,
        author=str(entry.get("author", "")).strip(),
        # The title is part of the post, not decoration around it: on a
        # link post it is the whole of what the author wrote.
        text=f"{title}\n\n{body}".strip() if body else title,
        published_at=_published(entry),
        signals=signals,
    )


def _body(entry: Any) -> str:
    """The entry's own prose, stripped of the markup feeds wrap it in."""
    raw = ""
    if entry.get("content"):
        raw = entry["content"][0].get("value", "")
    elif entry.get("summary"):
        raw = entry["summary"]
    text = _SPACE.sub(" ", _TAGS.sub(" ", html_mod.unescape(raw))).strip()
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
        return datetime.datetime(y, mo, d, h, mi, sec, tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None
