"""Adapters build search URLs and parse content without requesting pages."""

from urllib.parse import parse_qs, urlsplit

import pytest

from crawlme.platforms import instagram, reddit


@pytest.mark.parametrize("adapter", [instagram, reddit])
def test_adapter_builds_its_search_url(adapter):
    parts = urlsplit(adapter.search_url("gifts & toys"))
    assert parts.hostname == f"www.{adapter.DOMAIN}"
    assert parse_qs(parts.query)["q"] == ["gifts & toys"]
    if adapter is instagram:
        assert parts.path == "/explore/search/keyword/"
    else:
        assert parts.path == "/search/"
        assert parse_qs(parts.query)["sort"] == ["new"]


def test_reddit_search_parses_posts():
    html = (
        '<shreddit-post permalink="/r/test/comments/abc/gifts/" post-title="Free gifts" '
        'created-timestamp="2026-11-09T12:00:00Z"></shreddit-post>'
    )
    posts = reddit.parse_search(html, reddit.search_url("gifts"), [])
    assert len(posts) == 1
    assert posts[0].text == "Free gifts"
    assert posts[0].permalink == "https://www.reddit.com/r/test/comments/abc/gifts/"
    assert posts[0].published_at is not None


@pytest.mark.parametrize(
    ("seed", "author", "covered"),
    [
        ("https://www.instagram.com/TorontoDiscounts/?hl=en", "torontodiscounts", True),
        ("https://www.instagram.com/torontodiscounts/", "torontodiscounts_other", False),
        ("https://www.instagram.com/torontodiscounts/p/OLD/", "torontodiscounts", False),
        ("https://example.com/torontodiscounts/", "torontodiscounts", False),
    ],
)
def test_instagram_source_coverage(seed, author, covered):
    from crawlme.platforms.models import FeedItem

    post = FeedItem(f"https://www.instagram.com/{author}/p/NEW/", "instagram", author=author)
    assert instagram.seed_contains(seed, post) is covered


def test_reddit_source_coverage():
    from crawlme.platforms.models import FeedItem

    post = FeedItem("https://www.reddit.com/r/Deals/comments/abc/post/", "reddit", author="writer")
    assert reddit.seed_contains("https://www.reddit.com/r/deals/", post)
    assert reddit.seed_contains("https://www.reddit.com/user/writer/", post)
    assert not reddit.seed_contains("https://www.reddit.com/r/deals_other/", post)


def test_reddit_search_reads_rendered_search_cards():
    import datetime
    from pathlib import Path

    html = Path(__file__).with_name("data").joinpath("reddit_search.html").read_text()
    posts = reddit.parse_search(html, reddit.search_url("Toronto events"), [])
    assert len(posts) == 2
    assert posts[0].item_id == "t3_1xztulx"
    assert posts[0].author == "readingitnowonline"
    assert posts[0].text == "Event Cancelled: The Unauthorized Hallmark(ish) Parody Musical"
    assert posts[0].published_at == datetime.datetime(2026, 11, 18, 23, 12, 57, 62000, tzinfo=datetime.timezone.utc)
    assert all(p.text and p.published_at for p in posts)
    assert len({p.permalink for p in posts}) == 2


@pytest.mark.parametrize("metadata", ["{invalid", "[]", '{"profile": null}'])
def test_reddit_search_keeps_posts_without_author_metadata(metadata):
    from pathlib import Path

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(Path(__file__).with_name("data").joinpath("reddit_search.html").read_text(), "lxml")
    card = soup.select_one('[data-testid="search-sdui-post"]')
    card["data-faceplate-tracking-context"] = metadata
    card.select_one("time").decompose()
    card.select_one('[data-testid="post-title-text"]').decompose()
    posts = reddit.parse_search(str(card), reddit.search_url("events"), [])
    assert len(posts) == 1
    assert posts[0].author == ""
    assert posts[0].text == "Event Cancelled: The Unauthorized Hallmark(ish) Parody Musical"
    assert posts[0].published_at is not None
