"""Reddit: a listing of permalinks, a post, and the replies under it.

The route was not obvious and three cheaper ones are closed.  A feed
gives the title and a blurb and never the body; `old.reddit.com` answers
with a login wall; appending `.json` to a permalink answers 403.  All
three were measured with a browser user agent as well as ours, so none
of it is the crawler being turned away for what it calls itself.  What
is left is the same route Instagram takes: render the page.

Unlike Instagram it needs no session, and unlike Instagram its markup
says what things are.  Posts and comments are custom elements carrying
their own attributes, so nothing here depends on a class name -- the
obfuscated ones a rendered page is full of change without notice, and an
adapter pinned to them fails silently.
"""

from __future__ import annotations

import datetime
import html as html_mod
import logging
import re
from urllib.parse import urlparse

from crawlme.digest.feed.base import FeedItem, Listing, PageProblem
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

# One card on a listing.  It states the title, the score, how many
# replies it drew and when it was posted, which is what decides whether
# the post is worth a request of its own.  Reading only the href instead
# leaves the ranker judging a slug.
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
    """The host says it is ours; the markup says we can read it.

    Both, because the host alone is not enough here.  Reddit serves an
    8KB shell to anything that does not run JavaScript, and that shell
    carries none of these elements.  Claiming it anyway would mean
    parsing nothing out of a page nobody else got to read, and a run
    that fetched a subreddit with plain HTTP would report a quiet
    week -- every week.

    Not claiming it lets the page fall through to being read as a page,
    which is at least honest about having found nothing in it.
    """
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
    """Every post on the page, deduplicated, order kept.

    A subreddit shows its own posts and nothing else, so all of them are
    the listing's own -- there is no equivalent of a post that merely
    tagged the account.

    Each card is read for what it states about itself.  A permalink on
    its own leaves the ranker deciding from a URL slug, and a run whose
    seed was a busy subreddit dropped all fifty-two candidates that way:
    the titles were there on the page it had already paid for.
    """
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
    logger.info("reddit.parsed url=%s permalinks=%d titled=%d", url, len(items), titled)
    return Listing(own=items)


def parse_item(html: str, url: str = "") -> FeedItem | None:
    """The post itself, which makes the page a leaf.

    Decided from the address, not from the markup.  A subreddit listing
    carries a <shreddit-post> element for every card on it, so asking
    the markup answers yes for both kinds of page -- and a listing read
    as an item is a leaf, which means every subreddit yields no
    candidates at all while the run reports itself healthy.

    A post with no body is still a post: a link submission carries its
    title and nothing else, and treating that as "not an item" would
    send the harvester looking for permalinks on a page that is one.
    """
    if not _POST_URL.search(url):
        return None
    body = _POST_BODY.search(html)
    return FeedItem(
        permalink=url,
        platform=PLATFORM,
        text=_text_of(body.group(1)) if body else "",
    )


def next_page(html: str, url: str) -> str:
    """`?after=<last post>`, which the rendered site still honours.

    Measured over three pages of one subreddit: 79 posts each, 227 after
    deduplication, so a cursor reaches roughly three times what one page
    holds. The order it advances is the listing's, not time: all three
    pages spanned the same month, which is why TIME_HORIZON cannot stop
    a subreddit the way it stops a strictly ordered feed.
    """
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
    """When the card says the post appeared, if it says so at all.

    Recency is half of what ranking asks about a feed, and a candidate
    without it is judged as though it had no date rather than as
    something old.
    """
    if not raw:
        return None
    # Reddit writes the offset without a colon, which fromisoformat
    # rejects before 3.11.  Left as-is every card came back undated.
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", raw.strip().replace("Z", "+00:00"))
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
