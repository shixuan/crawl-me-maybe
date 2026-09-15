"""Parse rendered Reddit listing cards and post pages without a saved session."""

from __future__ import annotations

import datetime
import html as html_mod
import logging
import re
from urllib.parse import urlparse

from crawlme.adapters.base import FeedItem, Listing, PageProblem
from crawlme.schemas import Page, Payload

logger = logging.getLogger(__name__)

PLATFORM = "reddit"
DOMAIN = "reddit.com"

# A listing hands out one screen and loads more as it is scrolled.  Two
# turns brought back 19 permalinks, which is a week of most subreddits.
SCROLLS = 2

# Reading is open: no account, no session.
NEEDS_SESSION = False

# But not without a browser: plain HTTP gets an 8KB shell.
NEEDS_RENDERING = True

# A permalink looks like /r/<sub>/comments/<id>/<slug>/, and that shape
# is what separates a post from every other Reddit URL.
_PERMALINK = re.compile(r'href="(/r/[^/"]+/comments/[^"?#]+)"')

# Listing cards provide text and dates for ranking before fetching the post.
_POST_CARD = re.compile(r"<shreddit-post\s([^>]*)>", re.S)

# The thing id of a card, in page order. The last one is the cursor.
_CARD_ID = re.compile(r'<shreddit-post[^>]*\bid="(t3_[a-z0-9]+)"')

# The same shape, applied to the address a page was fetched from.  It
# is what tells a post from the listing that pointed at it.
_POST_URL = re.compile(r"/r/[^/]+/comments/[^/]+")

# The post's own body, keyed by its thing id rather than by any class.
_POST_BODY = re.compile(r'<div[^>]*\bid="t3_[^"]*-post-rtjson-content"[^>]*>(.*?)</div>', re.S)

# Attribute pairs off one element's opening tag.
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


def claims_url(url: str) -> bool:
    """Reddit is one of the platforms an address does identify."""
    host = (urlparse(url).hostname or "").lower()
    return host == DOMAIN or host.endswith("." + DOMAIN)


def claims(page: Page, document: str) -> bool:
    """Require both the Reddit host and recognized rendered markup."""
    return claims_url(page.url.canonical) and "shreddit-" in document


def problem(html: str) -> PageProblem | None:
    """Whether the page is a refusal rather than content."""
    if not html:
        return PageProblem.UNAVAILABLE
    head = html[:4000]
    if "/login" in head and "shreddit-app" not in html:
        return PageProblem.LOGIN_REQUIRED
    if "you've been blocked by network security" in html.lower():
        return PageProblem.BLOCKED
    if re.search(r"(?i)\b(this (community|subreddit) (is|has been) (private|banned))", head):
        return PageProblem.UNAVAILABLE
    return None


def parse_listing(html: str, url: str, payloads: list[Payload]) -> Listing:
    """Read listing cards with text and dates, deduplicated in document order."""
    seen: dict[str, FeedItem] = {}
    for m in _POST_CARD.finditer(html):
        attrs = dict(_ATTR.findall(m.group(1)))
        link = attrs.get("permalink", "")
        if not _POST_URL.search(link):
            continue
        permalink = "https://www." + DOMAIN + link.rstrip("/") + "/"
        seen.setdefault(
            permalink,
            FeedItem(
                permalink=permalink,
                platform=PLATFORM,
                item_id=attrs.get("id", ""),
                author=attrs.get("author", ""),
                text=_unescape(attrs.get("post-title", "")),
                published_at=_timestamp(attrs.get("created-timestamp")),
            ),
        )
    # Anything the cards missed still counts as a post: the href shape
    # is the older signal and holds when the markup moves on.
    for m in _PERMALINK.finditer(html):
        permalink = "https://www." + DOMAIN + m.group(1).rstrip("/") + "/"
        seen.setdefault(permalink, FeedItem(permalink=permalink, platform=PLATFORM))
    items = list(seen.values())
    titled = sum(1 for i in items if i.text)
    logger.debug("reddit.parsed url=%s permalinks=%d titled=%d", url, len(items), titled)
    return Listing(own=items)


def parse_item(html: str, url: str = "") -> FeedItem | None:
    """Recognize post URLs, including link submissions with no body.

    Listings also contain post elements, so markup alone cannot identify a leaf."""
    if not _POST_URL.search(url):
        return None
    body = _POST_BODY.search(html)
    return FeedItem(
        permalink=url,
        platform=PLATFORM,
        text=_text_of(body.group(1)) if body else "",
    )


def next_page(html: str, url: str) -> str:
    """Build a pagination cursor from the last post. Listing order need not be chronological."""
    if _POST_URL.search(url):
        return ""
    ids = _CARD_ID.findall(html)
    if not ids:
        return ""
    base = url.split("?", 1)[0]
    return f"{base}?after={ids[-1]}"


def keeps_payload(url: str, content_type: str) -> bool:
    """Nothing: the rendered page already states the post and replies."""
    return False


def _text_of(fragment: str) -> str:
    """Visible text of one markup fragment, whitespace collapsed."""
    return " ".join(html_mod.unescape(re.sub(r"(?s)<[^>]+>", " ", fragment)).split())


def _unescape(raw: str) -> str:
    """An attribute value as it reads, entities resolved."""
    return html_mod.unescape(raw).strip()


def _timestamp(raw: str | None) -> datetime.datetime | None:
    """Read the publication time declared by a listing card, or None."""
    if not raw:
        return None
    # Reddit writes the offset without a colon, which fromisoformat
    # rejects before 3.11.  Left as-is every card came back undated.
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", raw.strip().replace("Z", "+00:00"))
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
