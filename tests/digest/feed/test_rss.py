from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from crawlme.digest.feed import rss
from crawlme.digest.harvest import PageHarvester
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.schemas import URL, Page

pytest.importorskip("feedparser")

_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Weekend giveaway</title>
    <link href="https://example.com/post/1"/>
    <updated>2026-08-20T03:54:30+00:00</updated>
    <author><name>/u/someone</name></author>
    <content type="html">&lt;p&gt;Free bubble tea at the Queen Street shop all weekend,
      first fifty customers only, bring the code from this post.&lt;/p&gt;</content>
  </entry>
  <entry>
    <title>A link post</title>
    <link href="https://example.com/post/2"/>
    <content type="html">submitted by /u/x [link] [comments]</content>
  </entry>
</feed>"""

_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title>Only a title</title><link>https://example.com/p/3</link></item>
</channel></rss>"""

_HTML = b"""<!DOCTYPE html><html><head><title>Not a feed</title></head>
<body>The word feed appears here, and so does rss.<a href="/x">x</a></body></html>"""


def _page(tmp_path: Path, body: bytes, url: str = "https://example.com/feed") -> Page:
    raw = tmp_path / "doc"
    raw.write_bytes(body)
    return Page(
        url_key="k",
        url=URL(raw=url, canonical=url, url_key="k", domain="example.com", reg_domain="example.com"),
        raw_html_path=str(raw),
    )


@pytest.mark.parametrize(
    ("body", "claimed"),
    [
        (_ATOM, True),
        (_RSS, True),
        # An HTML page that talks about feeds is not one. The root
        # element is the only reliable signal, so only the root counts.
        (_HTML, False),
    ],
)
def test_claims_reads_the_root_element(tmp_path: Path, body: bytes, claimed: bool) -> None:
    page = _page(tmp_path, body)
    assert rss.claims(page, body.decode()) is claimed


def test_no_url_is_ever_claimed() -> None:
    """Measured against seven real feeds, one ended in .rss.

    Guessing from the address would be wrong five times in seven, so
    this adapter stays out of every check that runs before a fetch.
    """
    for url in ("https://x.com/feed", "https://x.com/a.rss", "https://x.com/atom.xml"):
        assert rss.claims_url(url) is False


def test_an_entry_arrives_carrying_its_own_text(tmp_path: Path) -> None:
    """The funnel used to rank a bare URL while the post sat in the feed."""
    out = PageHarvester(Canonicalizer(), [rss]).harvest(_page(tmp_path, _ATOM), depth=0)
    assert out.listing
    first = out.candidates[0]
    assert first.text.startswith("Weekend giveaway")
    assert "bubble tea" in first.text
    assert first.posted_at == datetime.datetime(2026, 8, 20, 3, 54, 30, tzinfo=datetime.timezone.utc)
    assert first.signals["account"] == "/u/someone"
    assert first.signals["platform"] == "rss"
    assert first.depth == 1


def test_a_link_post_keeps_its_title_and_drops_the_boilerplate(tmp_path: Path) -> None:
    """ "submitted by /u/name [link] [comments]" is the feed talking, not
    the author: it must not read as a post with a body."""
    out = PageHarvester(Canonicalizer(), [rss]).harvest(_page(tmp_path, _ATOM), depth=0)
    assert out.candidates[1].text == "A link post"


def test_an_entry_without_a_date_says_so(tmp_path: Path) -> None:
    """None has to stay distinguishable from a guess: the time window
    filters on this, and an invented date drops real posts silently."""
    out = PageHarvester(Canonicalizer(), [rss]).harvest(_page(tmp_path, _RSS), depth=0)
    assert out.candidates[0].posted_at is None
    assert out.candidates[0].text == "Only a title"


def test_a_feed_is_never_an_item() -> None:
    """A feed document lists posts; it is never one of them."""
    assert rss.parse_item(_ATOM.decode(), "https://example.com/feed") is None


def test_an_html_page_falls_through_to_links(tmp_path: Path) -> None:
    """Nobody claims it, so it is read the way a link graph reads."""
    out = PageHarvester(Canonicalizer(), [rss]).harvest(_page(tmp_path, _HTML), depth=0)
    assert not out.listing
    assert [c.url.canonical for c in out.candidates] == ["https://example.com/x"]
