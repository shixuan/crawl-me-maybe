"""Harvesters: what a page yields, per kind of source.

The engine calls one of these instead of the link extractor it used to
hardcode. The interesting property is the asymmetry between them: a link
graph treats every page as a source of more pages, and a feed does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crawlme.config import Settings
from crawlme.digest.feed import instagram
from crawlme.digest.feed.base import PageProblem
from crawlme.digest.harvest import FeedHarvester, LinkHarvester
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

#: A real profile whose grid holds no posts: an account that simply did
#: not post, which must stay distinguishable from one that was refused.
_EMPTY_LISTING = b"""<html><body><main>no posts yet</main></body></html>"""

_POST = b"""<html><head>
<meta property="og:description" content="103 likes, 0 comments - mollytea_canada on August 13, 2026: &quot;free tea">
</head><body><time datetime="2026-08-13T20:03:29.000Z"></time></body></html>"""


def _page(tmp_path: Path, html: bytes, url: str) -> Page:
    raw = tmp_path / "page.html"
    raw.write_bytes(html)
    return Page(
        url_key="src1",
        # The real domain, because a feed harvester decides from it
        # whether the page is even its platform's.
        url=URL(raw=url, canonical=url, url_key="src1", reg_domain=Canonicalizer().canonicalize(url, url).reg_domain),
        raw_html_path=str(raw),
    )


#: link graph ------------------------------------------------------------


def test_links_become_candidates_with_resolved_urls(tmp_path: Path) -> None:
    page = _page(tmp_path, _LINKS_HTML, "https://example.com/dir/page")
    out = LinkHarvester(Canonicalizer()).harvest(page, depth=2).candidates
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
    first = LinkHarvester(Canonicalizer()).harvest(page, depth=0).candidates[0]
    assert first.anchor == "first"
    assert first.text == "", "a link carries no content of its own"


#: feed ------------------------------------------------------------------


def test_a_listing_yields_its_post_permalinks(tmp_path: Path) -> None:
    page = _page(tmp_path, _LISTING, "https://www.instagram.com/mollytea_canada/")
    out = FeedHarvester(instagram, Canonicalizer()).harvest(page, depth=0).candidates
    assert len(out) == 3
    assert all(c.signals["platform"] == "instagram" for c in out)
    assert all(c.url.reg_domain == "instagram.com" for c in out)
    assert all(len(c.url.url_key) == 16 for c in out), "same key shape as every other source"


def test_a_listing_marks_posts_that_only_tagged_the_account(tmp_path: Path) -> None:
    """Kept, but distinguishable: a reviewer's post is not the shop's."""
    page = _page(tmp_path, _LISTING, "https://www.instagram.com/mollytea_canada/")
    out = FeedHarvester(instagram, Canonicalizer()).harvest(page, depth=0).candidates
    tagged = {c.url.canonical: c.signals["tagged_only"] for c in out}
    assert tagged["https://www.instagram.com/mollytea_canada/p/AAA111/"] is False
    assert tagged["https://www.instagram.com/hellofoodbaby_/p/CCC333/"] is True


def test_a_post_yields_nothing(tmp_path: Path) -> None:
    """A post is a leaf: its caption is the product, not a pointer on."""
    page = _page(tmp_path, _POST, "https://www.instagram.com/p/AAA111/")
    assert FeedHarvester(instagram, Canonicalizer()).harvest(page, depth=0).candidates == []


def test_a_page_that_is_not_content_says_why(tmp_path: Path) -> None:
    """A renamed account must not read as an account with a quiet week.

    Empty was the only answer available before, so the two were the same
    answer: the harvest now carries the reason it came back empty.
    """
    page = _page(tmp_path, b"<html>Sorry, this page isn't available</html>", "https://www.instagram.com/gone/")
    out = FeedHarvester(instagram, Canonicalizer()).harvest(page, depth=0)
    assert out.candidates == []
    assert out.problem is PageProblem.UNAVAILABLE
    assert not out.problem.refuses_the_run, "one gone account must not end a thirty-account run"


def test_a_quiet_account_is_not_a_problem(tmp_path: Path) -> None:
    """Empty and refused have to stay distinguishable in both directions."""
    page = _page(tmp_path, _EMPTY_LISTING, "https://www.instagram.com/quiet/")
    out = FeedHarvester(instagram, Canonicalizer()).harvest(page, depth=0)
    assert out.candidates == []
    assert out.problem is None


def test_a_block_is_about_the_crawl_not_the_page(tmp_path: Path) -> None:
    """Rate limiting and a dead session settle every request that follows."""
    for html, expected in (
        (b"<html>Please wait a few minutes before you try again.</html>", PageProblem.BLOCKED),
        (b'<html><form action="/accounts/login/">Log in</form></html>', PageProblem.LOGIN_REQUIRED),
    ):
        page = _page(tmp_path, html, "https://www.instagram.com/someone/")
        out = FeedHarvester(instagram, Canonicalizer()).harvest(page, depth=0)
        assert out.problem is expected
        assert out.problem.refuses_the_run


def test_a_page_with_no_stored_html_yields_nothing(tmp_path: Path) -> None:
    page = Page(url_key="k", url=URL(raw="https://x/", canonical="https://x/", url_key="k"))
    assert FeedHarvester(instagram, Canonicalizer()).harvest(page, depth=0).candidates == []


#: wiring ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("links", LinkHarvester), ("instagram", FeedHarvester)],
)
def test_factory_picks_the_harvester_from_settings(kind: str, expected: type) -> None:
    assert isinstance(_build_harvester(Settings(source_kind=kind), Canonicalizer()), expected)


def test_links_is_the_default() -> None:
    assert isinstance(_build_harvester(Settings(), Canonicalizer()), LinkHarvester)


def test_a_page_from_another_platform_yields_nothing(tmp_path: Path) -> None:
    """A crawl wanders off: an analyzer endorses the shop's own site.

    Being a leaf there is a policy, not luck. A site that happens to use
    the platform's path shape would otherwise hand back candidates
    pointing at the wrong host entirely.
    """
    trap = b"""<html><body>
    <a href="/p/AAA111/">looks like a post</a>
    <a href="/p/BBB222/">so does this</a>
    </body></html>"""
    page = _page(tmp_path, trap, "https://mollyteaca.com/promotions")
    assert FeedHarvester(instagram, Canonicalizer()).harvest(page, depth=0).candidates == []


def test_the_platform_check_reads_the_adapter_not_a_hardcoded_name(tmp_path: Path) -> None:
    """Swapping the adapter swaps which pages are ours, with no edit here."""

    class _Elsewhere:
        PLATFORM = "elsewhere"
        DOMAIN = "elsewhere.example"

        problem = staticmethod(instagram.problem)
        parse_item = staticmethod(instagram.parse_item)
        parse_listing = staticmethod(instagram.parse_listing)

    page = _page(tmp_path, _LISTING, "https://www.instagram.com/mollytea_canada/")
    assert FeedHarvester(_Elsewhere(), Canonicalizer()).harvest(page, depth=0).candidates == []
