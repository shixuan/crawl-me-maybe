"""Harvesters: what a page yields, per kind of source.

The engine calls one of these instead of the link extractor it used to
hardcode. The interesting property is the asymmetry between them: a link
graph treats every page as a source of more pages, and a feed does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crawlme.config import Settings
from crawlme.digest.harvest import InstagramHarvester, LinkHarvester
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.scheduler.factory import _build_harvester
from crawlme.schemas import URL, Page

_LINKS_HTML = b"""<html><body><article>
<h2>Rust</h2><p>Body text long enough to keep.</p>
<a href="/one">first</a> <a href="../two">second</a> <a href="https://other.com/x">third</a>
</article></body></html>"""

_LISTING = b"""<html><body>
<a href="/mollytea_canada/p/AAA111/">a</a>
<a href="/mollytea_canada/p/BBB222/">b</a>
<a href="/hellofoodbaby_/p/CCC333/">tagged</a>
</body></html>"""

_POST = b"""<html><head>
<meta property="og:description" content="103 likes, 0 comments - mollytea_canada on August 13, 2026: &quot;free tea">
</head><body><time datetime="2026-08-13T20:03:29.000Z"></time></body></html>"""


def _page(tmp_path: Path, html: bytes, url: str) -> Page:
    raw = tmp_path / "page.html"
    raw.write_bytes(html)
    return Page(
        url_key="src1",
        url=URL(raw=url, canonical=url, url_key="src1", reg_domain="example.com"),
        raw_html_path=str(raw),
    )


#: link graph ------------------------------------------------------------


def test_links_become_candidates_with_resolved_urls(tmp_path: Path) -> None:
    page = _page(tmp_path, _LINKS_HTML, "https://example.com/dir/page")
    out = LinkHarvester(Canonicalizer()).harvest(page, depth=2)
    assert len(out) == 3
    assert all(c.depth == 3 for c in out), "candidates sit one level below their source"
    assert all(c.source_url_key == "src1" for c in out)
    assert {c.url.canonical for c in out} == {
        "https://example.com/one",
        "https://example.com/two",
        "https://other.com/x",
    }


def test_links_carry_their_business_card(tmp_path: Path) -> None:
    """A link has no text of its own, so the proxies are all there is."""
    page = _page(tmp_path, _LINKS_HTML, "https://example.com/dir/page")
    first = LinkHarvester(Canonicalizer()).harvest(page, depth=0)[0]
    assert first.anchor == "first"
    assert first.text == "", "a link carries no content of its own"


#: feed ------------------------------------------------------------------


def test_a_listing_yields_its_post_permalinks(tmp_path: Path) -> None:
    page = _page(tmp_path, _LISTING, "https://www.instagram.com/mollytea_canada/")
    out = InstagramHarvester(Canonicalizer()).harvest(page, depth=0)
    assert len(out) == 3
    assert all(c.signals["platform"] == "instagram" for c in out)
    assert all(c.url.reg_domain == "instagram.com" for c in out)
    assert all(len(c.url.url_key) == 16 for c in out), "same key shape as every other source"


def test_a_listing_marks_posts_that_only_tagged_the_account(tmp_path: Path) -> None:
    """Kept, but distinguishable: a reviewer's post is not the shop's."""
    page = _page(tmp_path, _LISTING, "https://www.instagram.com/mollytea_canada/")
    out = InstagramHarvester(Canonicalizer()).harvest(page, depth=0)
    tagged = {c.url.canonical: c.signals["tagged_only"] for c in out}
    assert tagged["https://www.instagram.com/mollytea_canada/p/AAA111/"] is False
    assert tagged["https://www.instagram.com/hellofoodbaby_/p/CCC333/"] is True


def test_a_post_yields_nothing(tmp_path: Path) -> None:
    """A post is a leaf: its caption is the product, not a pointer on."""
    page = _page(tmp_path, _POST, "https://www.instagram.com/p/AAA111/")
    assert InstagramHarvester(Canonicalizer()).harvest(page, depth=0) == []


def test_a_page_that_is_not_content_yields_nothing(tmp_path: Path) -> None:
    """A renamed account must not read as an account with a quiet week."""
    page = _page(tmp_path, b"<html>Sorry, this page isn't available</html>", "https://www.instagram.com/gone/")
    assert InstagramHarvester(Canonicalizer()).harvest(page, depth=0) == []


def test_a_page_with_no_stored_html_yields_nothing(tmp_path: Path) -> None:
    page = Page(url_key="k", url=URL(raw="https://x/", canonical="https://x/", url_key="k"))
    assert InstagramHarvester(Canonicalizer()).harvest(page, depth=0) == []


#: wiring ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("links", LinkHarvester), ("instagram", InstagramHarvester)],
)
def test_factory_picks_the_harvester_from_settings(kind: str, expected: type) -> None:
    assert isinstance(_build_harvester(Settings(source_kind=kind), Canonicalizer()), expected)


def test_links_is_the_default() -> None:
    assert isinstance(_build_harvester(Settings(), Canonicalizer()), LinkHarvester)
