"""The Reddit adapter, against markup taken from real pages.

The fixtures are trimmed: the custom elements and their bodies, without
the megabyte of styles a rendered page carries.  What is kept is exactly
what the adapter reads, so a fixture that stops matching means the
platform changed rather than that the file got stale.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crawlme.digest.feed import reddit
from crawlme.digest.feed.base import PageProblem
from crawlme.schemas import URL, Page

_DATA = Path(__file__).parent / "data"
_LISTING = (_DATA / "reddit_listing.html").read_text()
_POST = (_DATA / "reddit_post.html").read_text()

_POST_URL = "https://www.reddit.com/r/Python/comments/1vunmhj/vs_code_vs_pycharm/"
_LISTING_URL = "https://www.reddit.com/r/Python/top/"


def _page(url: str) -> Page:
    return Page(url_key="k", url=URL(raw=url, canonical=url, url_key="k", reg_domain="reddit.com"))


@pytest.mark.parametrize(
    ("url", "mine"),
    [
        ("https://www.reddit.com/r/Python/top/", True),
        ("https://old.reddit.com/r/Python/", True),
        ("https://reddit.com/r/Python/", True),
        ("https://example.com/r/Python/", False),
        ("https://notreddit.com/", False),
        ("https://reddit.com.evil.example/", False),
    ],
)
def test_claims_by_host(url, mine):
    assert reddit.claims_url(url) is mine


def test_a_listing_is_not_an_item():
    """A subreddit carries a <shreddit-post> element per card, so asking
    the markup answers yes for both kinds of page -- and a listing read
    as an item is a leaf, which means every subreddit yields nothing
    while the run reports itself healthy."""
    assert reddit.parse_item(_LISTING, _LISTING_URL) is None
    assert reddit.parse_item(_POST, _POST_URL) is not None


def test_a_listing_yields_its_permalinks():
    listing = reddit.parse_listing(_LISTING, _LISTING_URL, [])
    assert listing.all, "no permalinks found"
    assert all(i.permalink.startswith("https://www.reddit.com/r/") for i in listing.all)
    assert all("/comments/" in i.permalink for i in listing.all)
    assert len({i.permalink for i in listing.all}) == len(listing.all), "duplicates"
    assert not listing.others, "a subreddit shows only its own posts"


def test_an_item_carries_the_post_body():
    item = reddit.parse_item(_POST, _POST_URL)
    assert item is not None
    assert len(item.text or "") > 40
    assert "<" not in (item.text or ""), "markup leaked into the text"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("", PageProblem.UNAVAILABLE),
        ("<html>you've been blocked by network security</html>", PageProblem.BLOCKED),
        ("<html><head><meta url='/login'></head></html>", PageProblem.LOGIN_REQUIRED),
        ("<html>this community is private</html>", PageProblem.UNAVAILABLE),
    ],
)
def test_refusals_are_told_apart(html, expected):
    assert reddit.problem(html) is expected


def test_a_real_page_is_not_a_refusal():
    assert reddit.problem(_POST) is None
    assert reddit.problem(_LISTING) is None


def test_reading_needs_no_session():
    """Unlike Instagram: this is what lets a Reddit run go without one."""
    assert reddit.NEEDS_SESSION is False


def test_an_unrendered_shell_is_not_claimed():
    """Reddit serves 8KB of script to anything without a browser, and
    that shell carries none of these elements.  Claiming it anyway would
    mean a subreddit fetched over plain HTTP reports a quiet week --
    every week."""
    shell = "<html><head><title>Python</title></head><body></body></html>"
    assert reddit.claims(_page(_LISTING_URL), shell) is False
    assert reddit.claims(_page(_LISTING_URL), _LISTING) is True


def test_rendering_is_required_and_a_session_is_not():
    """Two different requirements: anyone may read Reddit, but not
    without a browser."""
    assert reddit.NEEDS_RENDERING is True
    assert reddit.NEEDS_SESSION is False


def test_a_listing_carries_what_each_card_states():
    """A permalink alone leaves the ranker reading a URL slug."""
    items = reddit.parse_listing(_LISTING, _LISTING_URL, []).own
    assert items
    titled = [i for i in items if i.text]
    assert titled, "no card was read for its title"
    first = titled[0]
    assert first.permalink.startswith("https://www.reddit.com/r/")
    assert first.item_id.startswith("t3_")
    assert first.author


def test_a_card_without_a_colon_in_its_offset_is_still_dated():
    """Reddit writes +0000, which fromisoformat rejects before 3.11.
    Unhandled, every candidate arrives undated and is ranked as though
    it had no date rather than as something recent."""
    stamped = [i for i in reddit.parse_listing(_LISTING, _LISTING_URL, []).own if i.published_at]
    assert stamped, "no card kept its timestamp"
    assert stamped[0].published_at is not None
    assert stamped[0].published_at.tzinfo is not None


def test_a_permalink_the_cards_missed_is_still_a_candidate():
    """The href shape is the older signal and holds when the markup
    moves on, so it stays as the floor under the card reader."""
    html = '<a href="/r/Python/comments/abc123/some_slug/">x</a>'
    items = reddit.parse_listing(html, _LISTING_URL, []).own
    assert [i.permalink for i in items] == ["https://www.reddit.com/r/Python/comments/abc123/some_slug/"]
