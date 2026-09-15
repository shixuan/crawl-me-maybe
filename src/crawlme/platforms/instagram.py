"""Parse Instagram post pages and listing payloads, with DOM fallback."""

from __future__ import annotations

import dataclasses
import datetime
import html as html_module
import json
import logging
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from crawlme.platforms.base import FeedItem, Listing, PageProblem
from crawlme.schemas import FetchResult, Page, Payload

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

# Bound the search so a missing alt attribute cannot consume the next entry.
# Alt text describes the image and may name the author and date, not the caption.
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


# Request additional grid pages through browser scrolling.
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
    """Ours by host.  A crawl wanders off a platform routinely, so this
    answers for the address rather than for the run."""
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


def next_page(html: str, url: str) -> str:
    """None yet: the cursor lives in an XHR the fetcher does not keep."""
    return ""


def keeps_payload(url: str, content_type: str) -> bool:
    """Keep GraphQL responses regardless of content type; grids may use text/javascript."""
    return "/graphql/query" in url


def extract_text(result: FetchResult) -> str | None:
    """Read only the requested parent post; unrelated payload posts are not fallback text."""
    url = result.url.canonical
    match = _SHORTCODE.search(urlsplit(url).path)
    if not claims_url(url) or match is None or result.status_code != 200:
        return None
    if result.final_url is not None:
        final = result.final_url.canonical
        final_match = _SHORTCODE.search(urlsplit(final).path)
        if not claims_url(final) or final_match is None or final_match[1] != match[1]:
            return None
    document = result.raw.decode("utf-8", "ignore")
    if problem(document) is not None:
        return None
    post = _posts_from_payloads(result.payloads, set()).get(match[1])
    if post is None:
        # Detail pages may embed the target post while captured responses contain only recommendations.
        posts: dict[str, _Post] = {}
        media_codes: set[str] = set()
        soup = BeautifulSoup(document, "html.parser")
        for script in soup.find_all("script", attrs={"type": "application/json"}):
            try:
                data = json.loads(script.string or "")
            except json.JSONDecodeError:
                continue
            _collect_posts(data, posts, media_codes)
        if match[1] not in media_codes:
            post = posts.get(match[1])
    return post.text if post is not None else None


# Distinguish the requested account grid from the viewer home timeline.
_GRID_ANSWER = "user_timeline_graphql_connection"


def _body_text(payload: Payload) -> str:
    return payload.body.decode("utf-8", "ignore")


def parse_listing(html: str, url: str, payloads: list[Payload]) -> Listing:
    """Read posts by shortcode and split them by owner.

    Prefer payload captions and timestamps. DOM entries are a fallback because
    scrolling can remove earlier posts from the rendered grid."""
    handle = _account_from_url(url).strip("/").lower()
    media_codes: set[str] = set()
    posts = _posts_from_payloads(payloads, media_codes)
    alts = {href: alt for href, alt in _GRID_ENTRY.findall(html)}
    # Use the account handle for ownership; image alt text may contain only a display name.
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
        if code in seen or code in media_codes:
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
    # Use the expected connection name for diagnostics; parse posts by their shape.
    answered = any(_GRID_ANSWER in _body_text(pl) for pl in payloads)
    return Listing(own=own, others=others, degraded=not answered)


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
    """Read a post caption and timestamp. Require post markers to exclude profile bios."""
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


# Allow variable whitespace before the caption quote in the description.
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


def _posts_from_payloads(payloads: list[Payload], media_codes: set[str]) -> dict[str, _Post]:
    """Index payload posts by shortcode, retaining caption, author and timestamp."""
    out: dict[str, _Post] = {}
    for payload in payloads:
        try:
            data = json.loads(payload.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("instagram.payload_unreadable url=%s", payload.url)
            continue
        _collect_posts(data, out, media_codes)
    return {code: post for code, post in out.items() if code not in media_codes}


def _collect_posts(node: object, out: dict[str, _Post], media_codes: set[str]) -> None:
    if isinstance(node, list):
        for child in node:
            _collect_posts(child, out, media_codes)
        return
    if not isinstance(node, dict):
        return
    code = node.get("code") or node.get("shortcode")
    text = _caption_text(node, media_codes)
    if isinstance(code, str):
        if text:
            user = node.get("user")
            author = user.get("username") if isinstance(user, dict) else ""
            out.setdefault(
                code,
                _Post(text.strip(), _taken_at(node.get("taken_at")), str(author or "")),
            )
    for key, child in node.items():
        # Carousel media belong to their parent, even when they have captions and codes.
        if key != "carousel_media":
            _collect_posts(child, out, media_codes)


def _caption_text(node: dict[str, object], media_codes: set[str]) -> str:
    parts: list[str] = []

    def collect(item: dict[str, object]) -> None:
        caption = item.get("caption")
        text = caption.get("text") if isinstance(caption, dict) else None
        if isinstance(text, str):
            text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
            if text and not any(text in previous for previous in parts):
                parts.append(text)
        children = item.get("carousel_media")
        if isinstance(children, list):
            for child in children:
                if not isinstance(child, dict):
                    continue
                code = child.get("code") or child.get("shortcode")
                if isinstance(code, str):
                    media_codes.add(code)
                collect(child)

    collect(node)
    return "\n\n".join(parts)


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
