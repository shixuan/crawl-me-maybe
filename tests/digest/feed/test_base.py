"""FeedItem: the boundary between an adapter and the pipeline."""

from __future__ import annotations

import datetime

from crawlme.digest.feed import FeedItem, Listing


def _item(**kw) -> FeedItem:
    base = dict(permalink="https://www.instagram.com/p/AAA111/", platform="instagram")
    base.update(kw)
    return FeedItem(**base)  # type: ignore[arg-type]


def test_caption_lands_in_text():
    """The funnel reads text, so the post's own words must land there."""
    c = _item(
        text="free drink today",
        author="mollytea_canada",
        item_id="AAA111",
        published_at=datetime.datetime(2026, 8, 13, tzinfo=datetime.timezone.utc),
        signals={"likes": 103},
    ).to_candidate()
    assert c.text == "free drink today"
    assert c.signals["platform"] == "instagram"
    assert c.signals["account"] == "mollytea_canada"
    assert c.signals["item_id"] == "AAA111"
    assert c.signals["likes"] == 103
    # Typed rather than in the bag: the funnel scores on it and the time
    # window filters on it, so a key read by name would fail silently.
    assert c.posted_at is not None and c.posted_at.date().isoformat() == "2026-08-13"
    assert "posted_at" not in c.signals


def test_domain_comes_from_the_permalink():
    """Adapters name the platform; the domain is derived, not declared."""
    assert _item().to_candidate().url.reg_domain == "instagram.com"


def test_item_without_text_converts():
    c = _item().to_candidate()
    assert c.text == ""
    assert c.signals == {"platform": "instagram"}


def test_listing_all_keeps_own_first():
    """Own posts lead, because a monitored account is what was asked for."""
    lst = Listing(own=["a", "b"], others=["c"])
    assert lst.all == ["a", "b", "c"]
