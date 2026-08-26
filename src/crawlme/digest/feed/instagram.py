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

import dataclasses
import datetime
import html as html_module
import json
import logging
import re
from urllib.parse import urlsplit

from crawlme.digest.feed.base import FeedItem, Listing, PageProblem
from crawlme.schemas import Page, Payload

logger = logging.getLogger(__name__)

PLATFORM = "instagram"
DOMAIN = "instagram.com"

# A wrong handle renders a full, healthy-looking page rather than a 404.
_UNAVAILABLE = ("sorry, this page", "isn't available", "page not found")
_BLOCKED = ("challenge_required", "checkpoint_required", "please wait a few minutes")
_LOGIN = ("loginform", "/accounts/login")

# The grid renders profile-scoped permalinks while the address bar shows
# the bare form. Matching only one reports zero posts on a full page.
_PERMALINK = re.compile(r'href="((?:/[A-Za-z0-9_.]+)?/(?:p|reel)/([A-Za-z0-9_-]+)/?)"')

# A grid entry is an anchor wrapping an img whose alt Instagram
# generates: `Photo shared by NAME on August 13, 2026 tagging @x. May be
# an image of tea and text.` It names the author, the day and roughly
# what is pictured, but never what the post says. Window-bounded so a
# missing alt cannot swallow the next entry's.
_GRID_ENTRY = re.compile(
    r'href="((?:/[A-Za-z0-9_.]+)?/(?:p|reel)/[A-Za-z0-9_-]+/?)"(?:(?!href=").){0,600}?alt="([^"]*)"',
    re.S,
)
_ALT_AUTHOR_DATE = re.compile(r"(?:shared by|Photo by)\s+(.+?)\s+on\s+([A-Z][a-z]+ \d{1,2}, \d{4})")
_SHORTCODE = re.compile(r"/(?:p|reel)/([A-Za-z0-9_-]+)")

_CAPTION_JSON = re.compile(r'"caption"\s*:\s*\{\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"')
_OG_DESCRIPTION = re.compile(r'<meta[^>]*property="og:description"[^>]*content="([^"]*)"', re.S)
_TIME_TAG = re.compile(r'<time[^>]*datetime="([^"]+)"')
# `103 likes, 0 comments - handle on August 13, 2026: "caption`
_POST_DESC = re.compile(r"([\d,]+)\s+likes?,\s*[\d,]+\s+comments?\s*-\s*([A-Za-z0-9_.]+)\s+on\s")


# A grid hands out one screen at a time, so a window of weeks sees a
# dozen posts without this.  Scrolling asks the page for its own next
# page; nothing is forged.
SCROLLS = 4

# Nothing here is readable logged out: the platform answers a stranger
# with its login page, whatever was asked for.
NEEDS_SESSION = True

# A timeline is built by script, not served as markup.
NEEDS_RENDERING = True


def claims_url(url: str) -> bool:
    """Ours by host, from the address alone."""
    host = urlsplit(url).hostname or ""
    return host == DOMAIN or host.endswith("." + DOMAIN)


def claims(page: Page, document: str) -> bool:
    """Ours by host.  A crawl wanders off a platform routinely: an
    analyzer endorses a shop's own site and that page arrives next."""
    return page.url.reg_domain == DOMAIN


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


def keeps_payload(url: str, content_type: str) -> bool:
    """The grid is built from a graphql answer, and that answer has the text."""
    return "json" in content_type and "/graphql/query" in url


def parse_listing(html: str, url: str, payloads: list[Payload]) -> Listing:
    """Read a grid into items, split by who posted them.

    Whose grid this is comes out of the URL here rather than being handed
    in, because reading an account out of a URL is as platform-shaped as
    the markup is: the reserved segments that are not accounts are
    Instagram's own list.

    The payload is the better source when there is one: it states what
    each post says, who posted it and when, none of which the grid
    renders. It also survives scrolling, and the grid does not -- the
    markup drops items as they leave the viewport, so a page scrolled for
    more posts ends up showing fewer of them.

    The markup is the fallback, and is all there is for a plain HTTP
    fetch or after the platform changes its response shape. Then the only
    text is Instagram's generated description of the image, which
    describes the picture rather than the offer in it, and this returns
    exactly what it always did.
    """
    handle = _account_from_url(url).strip("/").lower()
    posts = _posts_from_payloads(payloads)
    alts = {href: alt for href, alt in _GRID_ENTRY.findall(html)}
    # Ownership is decided by the account a post belongs to, never by the
    # name shown next to it: the grid's alt text carries a display name
    # ("MollyTeaCanada") where the handle is what a listing is keyed on.
    seen: dict[str, FeedItem] = {}
    owners: dict[str, str] = {}

    for code, post in posts.items():
        owner = (post.author or handle).lower()
        owners[code] = owner
        seen[code] = FeedItem(
            permalink=f"https://www.instagram.com/{owner}/p/{code}/",
            platform=PLATFORM,
            item_id=code,
            author=owner,
            text=post.text,
            published_at=post.taken_at,
        )
    for href, code in dict.fromkeys(_PERMALINK.findall(html)):
        if code in seen:
            continue
        owner = href.strip("/").split("/")[0].lower()
        alt = alts.get(href, "")
        author, posted = _from_alt(alt)
        owners[code] = owner
        seen[code] = FeedItem(
            permalink=_absolute(href),
            platform=PLATFORM,
            item_id=code,
            author=author or owner,
            text=alt,
            published_at=posted,
        )

    own = [i for c, i in seen.items() if owners[c] == handle]
    others = [i for c, i in seen.items() if owners[c] != handle]
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


# helpers ---------------------------------------------------------------


def _caption_from_json(html: str) -> str:
    """The inline JSON holds the full caption; the meta tag truncates it."""
    m = _CAPTION_JSON.search(html)
    if not m:
        return ""
    try:
        return str(json.loads(f'"{m.group(1)}"')).strip()
    except json.JSONDecodeError:
        return ""


# `... on August 13, 2026: "caption`. The gap between the colon and the
# quote is whatever the markup happened to wrap with, so it is matched
# loosely rather than as a literal `: "`.
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


@dataclasses.dataclass(frozen=True)
class _Post:
    text: str
    taken_at: datetime.datetime | None
    author: str


def _posts_from_payloads(payloads: list[Payload]) -> dict[str, _Post]:
    """Map post code -> what the response says about it.

    Found by shape rather than by path. The connection these live under
    is named for an internal API version and will be renamed; a post is
    recognisable without knowing where it sits, and looking for the shape
    keeps one rename from emptying the result.
    """
    out: dict[str, _Post] = {}
    for payload in payloads:
        try:
            data = json.loads(payload.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("instagram.payload_unreadable url=%s", payload.url)
            continue
        _collect_posts(data, out)
    return out


def _collect_posts(node: object, out: dict[str, _Post]) -> None:
    if isinstance(node, list):
        for child in node:
            _collect_posts(child, out)
        return
    if not isinstance(node, dict):
        return
    code = node.get("code") or node.get("shortcode")
    caption = node.get("caption")
    if isinstance(code, str) and isinstance(caption, dict):
        text = caption.get("text")
        if isinstance(text, str) and text.strip():
            user = node.get("user")
            author = user.get("username") if isinstance(user, dict) else ""
            out.setdefault(
                code,
                _Post(text.strip(), _taken_at(node.get("taken_at")), str(author or "")),
            )
    for child in node.values():
        _collect_posts(child, out)


def _taken_at(raw: object) -> datetime.datetime | None:
    """Instagram states the exact second; the grid alt text often states nothing."""
    if not isinstance(raw, int) or raw <= 0:
        return None
    try:
        return datetime.datetime.fromtimestamp(raw, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _absolute(href: str) -> str:
    return f"https://www.instagram.com{href}" if href.startswith("/") else href


def _account_from_url(url: str) -> str:
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)/?", url)
    handle = m.group(1) if m else ""
    return "" if handle in {"p", "reel", "explore"} else handle
