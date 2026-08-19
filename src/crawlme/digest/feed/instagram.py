"""Instagram markup: selectors, quirks, and nothing else.

Written against pages the Phase 0 probe actually captured rather than
against a guess, because every pattern below is a heuristic over a layout
nobody promised to keep stable.

Two page shapes matter. A profile is a grid of permalinks carrying only
Instagram's generated alt text, which names the author and the date but
not what the post says. A post page carries the caption and an exact
timestamp. That split is why a feed still wants a funnel: the grid is
weak content and cheap, the caption is strong content and costs one
request each.

Nothing here fetches, so all of it is testable against saved pages.
"""

from __future__ import annotations

import datetime
import html as html_module
import json
import re

from crawlme.digest.feed.base import FeedItem, Listing, PageProblem

PLATFORM = "instagram"
DOMAIN = "instagram.com"

#: A wrong handle renders a full, healthy-looking page rather than a 404.
_UNAVAILABLE = ("sorry, this page", "isn't available", "page not found")
_BLOCKED = ("challenge_required", "checkpoint_required", "please wait a few minutes")
_LOGIN = ("loginform", "/accounts/login")

#: The grid renders profile-scoped permalinks while the address bar shows
#: the bare form. Matching only one reports zero posts on a full page.
_PERMALINK = re.compile(r'href="((?:/[A-Za-z0-9_.]+)?/(?:p|reel)/([A-Za-z0-9_-]+)/?)"')

#: A grid entry is an anchor wrapping an img whose alt Instagram
#: generates: `Photo shared by NAME on August 13, 2026 tagging @x. May be
#: an image of tea and text.` It names the author, the day and roughly
#: what is pictured, but never what the post says. Window-bounded so a
#: missing alt cannot swallow the next entry's.
_GRID_ENTRY = re.compile(
    r'href="((?:/[A-Za-z0-9_.]+)?/(?:p|reel)/[A-Za-z0-9_-]+/?)"(?:(?!href=").){0,600}?alt="([^"]*)"',
    re.S,
)
_ALT_AUTHOR_DATE = re.compile(r"(?:shared by|Photo by)\s+(.+?)\s+on\s+([A-Z][a-z]+ \d{1,2}, \d{4})")
_SHORTCODE = re.compile(r"/(?:p|reel)/([A-Za-z0-9_-]+)")

_CAPTION_JSON = re.compile(r'"caption"\s*:\s*\{\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"')
_OG_DESCRIPTION = re.compile(r'<meta[^>]*property="og:description"[^>]*content="([^"]*)"', re.S)
_TIME_TAG = re.compile(r'<time[^>]*datetime="([^"]+)"')
#: `103 likes, 0 comments - handle on August 13, 2026: "caption`
_POST_DESC = re.compile(r"([\d,]+)\s+likes?,\s*[\d,]+\s+comments?\s*-\s*([A-Za-z0-9_.]+)\s+on\s")


def problem(html: str) -> PageProblem | None:
    """Name what is wrong with this page, or None if it holds content."""
    lowered = html.lower()
    if any(m in lowered for m in _UNAVAILABLE):
        return PageProblem.UNAVAILABLE
    if any(m in lowered for m in _BLOCKED):
        return PageProblem.BLOCKED
    if any(m in lowered for m in _LOGIN):
        return PageProblem.LOGIN_REQUIRED
    return None


def parse_listing(html: str, url: str) -> Listing:
    """Read a grid into items, split by who posted them.

    Whose grid this is comes out of the URL here rather than being handed
    in, because reading an account out of a URL is as platform-shaped as
    the markup is: the reserved segments that are not accounts are
    Instagram's own list.

    The generated alt text is kept as the item's text. It is weak, but it
    is what a listing has, and filtering on it is what stops the crawl
    paying one request per post to find out the same thing.
    """
    handle = _account_from_url(url).strip("/").lower()
    alts = {href: alt for href, alt in _GRID_ENTRY.findall(html)}
    own: list[FeedItem] = []
    others: list[FeedItem] = []
    for href, code in dict.fromkeys(_PERMALINK.findall(html)):
        owner = href.strip("/").split("/")[0].lower()
        alt = alts.get(href, "")
        author, posted = _from_alt(alt)
        item = FeedItem(
            permalink=_absolute(href),
            platform=PLATFORM,
            item_id=code,
            author=author or owner,
            text=alt,
            published_at=posted,
        )
        (own if owner == handle else others).append(item)
    return Listing(own=own, others=others)


def _from_alt(alt: str) -> tuple[str, datetime.datetime | None]:
    """Author and day, as the grid states them.

    Day precision only: the grid never gives a time. Good enough to decide
    whether a post is inside a week-wide window, which is all this is for.
    """
    m = _ALT_AUTHOR_DATE.search(alt)
    if not m:
        return "", None
    try:
        day = datetime.datetime.strptime(m.group(2), "%B %d, %Y")
    except ValueError:
        return m.group(1).strip(), None
    return m.group(1).strip(), day.replace(tzinfo=datetime.timezone.utc)


def parse_item(html: str, url: str = "") -> FeedItem | None:
    """Pull the caption and timestamp out of a post page.

    Returns None for anything that is not a post page. A profile page
    also carries an og:description, so parsing one as a post yields a
    plausible-looking item whose "caption" is the profile bio.
    """
    if problem(html):
        return None
    description = html_module.unescape(_first(_OG_DESCRIPTION, html))
    stats = _POST_DESC.search(description)
    published = _published_at(html)
    if stats is None and published is None:
        return None

    caption = _caption_from_json(html) or _caption_from_description(description)
    if not caption:
        return None

    signals: dict[str, object] = {}
    author = ""
    if stats is not None:
        signals["likes"] = int(stats.group(1).replace(",", ""))
        author = stats.group(2)
    return FeedItem(
        permalink=url,
        platform=PLATFORM,
        item_id=_first(_SHORTCODE, url),
        author=author or _account_from_url(url),
        text=caption,
        published_at=published,
        signals=signals,
    )


#: helpers ---------------------------------------------------------------


def _caption_from_json(html: str) -> str:
    """The inline JSON holds the full caption; the meta tag truncates it."""
    m = _CAPTION_JSON.search(html)
    if not m:
        return ""
    try:
        return str(json.loads(f'"{m.group(1)}"')).strip()
    except json.JSONDecodeError:
        return ""


#: `... on August 13, 2026: "caption`. The gap between the colon and the
#: quote is whatever the markup happened to wrap with, so it is matched
#: loosely rather than as a literal `: "`.
_DESC_CAPTION = re.compile(r':\s*["\u201c](.*)', re.S)


def _caption_from_description(description: str) -> str:
    """Fallback for when the JSON nesting changes but the meta tag does not."""
    m = _DESC_CAPTION.search(description)
    return m.group(1).strip().rstrip('"\u201d') if m else ""


def _published_at(html: str) -> datetime.datetime | None:
    raw = _first(_TIME_TAG, html)
    if not raw:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _first(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    return m.group(1) if m else ""


def _absolute(href: str) -> str:
    return f"https://www.instagram.com{href}" if href.startswith("/") else href


def _account_from_url(url: str) -> str:
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)/?", url)
    handle = m.group(1) if m else ""
    return "" if handle in {"p", "reel", "explore"} else handle
