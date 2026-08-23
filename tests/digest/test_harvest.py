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
from crawlme.digest.harvest import PageHarvester
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
    out = PageHarvester(Canonicalizer()).harvest(page, depth=2).candidates
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
    first = PageHarvester(Canonicalizer()).harvest(page, depth=0).candidates[0]
    assert first.anchor == "first"
    assert first.text == "", "a link carries no content of its own"


#: feed ------------------------------------------------------------------


def test_listing_yields_permalinks(tmp_path: Path) -> None:
    page = _page(tmp_path, _LISTING, "https://www.instagram.com/mollytea_canada/")
    out = PageHarvester(Canonicalizer(), [instagram]).harvest(page, depth=0).candidates
    assert len(out) == 3
    assert all(c.signals["platform"] == "instagram" for c in out)
    assert all(c.url.reg_domain == "instagram.com" for c in out)
    assert all(len(c.url.url_key) == 16 for c in out), "same key shape as every other source"


def test_listing_marks_tagged_only_posts(tmp_path: Path) -> None:
    """Kept, but distinguishable: a reviewer's post is not the shop's."""
    page = _page(tmp_path, _LISTING, "https://www.instagram.com/mollytea_canada/")
    out = PageHarvester(Canonicalizer(), [instagram]).harvest(page, depth=0).candidates
    tagged = {c.url.canonical: c.signals["tagged_only"] for c in out}
    assert tagged["https://www.instagram.com/mollytea_canada/p/AAA111/"] is False
    assert tagged["https://www.instagram.com/hellofoodbaby_/p/CCC333/"] is True


def test_post_yields_nothing(tmp_path: Path) -> None:
    """A post is a leaf: its caption is the product, not a pointer on."""
    page = _page(tmp_path, _POST, "https://www.instagram.com/p/AAA111/")
    assert PageHarvester(Canonicalizer(), [instagram]).harvest(page, depth=0).candidates == []


def test_not_content_says_why(tmp_path: Path) -> None:
    """A renamed account must not read as an account with a quiet week.

    Empty was the only answer available before, so the two were the same
    answer: the harvest now carries the reason it came back empty.
    """
    page = _page(tmp_path, b"<html>Sorry, this page isn't available</html>", "https://www.instagram.com/gone/")
    out = PageHarvester(Canonicalizer(), [instagram]).harvest(page, depth=0)
    assert out.candidates == []
    assert out.problem is PageProblem.UNAVAILABLE
    assert not out.problem.refuses_the_run, "one gone account must not end a thirty-account run"


def test_quiet_account_is_not_a_problem(tmp_path: Path) -> None:
    """Empty and refused have to stay distinguishable in both directions."""
    page = _page(tmp_path, _EMPTY_LISTING, "https://www.instagram.com/quiet/")
    out = PageHarvester(Canonicalizer(), [instagram]).harvest(page, depth=0)
    assert out.candidates == []
    assert out.problem is None


def test_block_refuses_the_crawl(tmp_path: Path) -> None:
    """Rate limiting and a dead session settle every request that follows."""
    for html, expected in (
        (b"<html>Please wait a few minutes before you try again.</html>", PageProblem.BLOCKED),
        (b'<html><form action="/accounts/login/">Log in</form></html>', PageProblem.LOGIN_REQUIRED),
    ):
        page = _page(tmp_path, html, "https://www.instagram.com/someone/")
        out = PageHarvester(Canonicalizer(), [instagram]).harvest(page, depth=0)
        assert out.problem is expected
        assert out.problem.refuses_the_run


def test_missing_html_yields_nothing(tmp_path: Path) -> None:
    page = Page(url_key="k", url=URL(raw="https://x/", canonical="https://x/", url_key="k"))
    assert PageHarvester(Canonicalizer(), [instagram]).harvest(page, depth=0).candidates == []


#: wiring ----------------------------------------------------------------


@pytest.mark.parametrize(("session", "platforms"), [("", []), ("./s.json", ["instagram"])])
def test_the_session_decides_which_platforms_are_read(session: str, platforms: list[str]) -> None:
    """One harvester now; what changes is which platforms it may read,
    and having credentials for one is what makes reading it possible."""
    h = _build_harvester(Settings(browser_storage_state=session), Canonicalizer())
    assert [a.PLATFORM for a in h._adapters if a.PLATFORM != "rss"] == platforms


def test_a_link_graph_reads_no_platform_but_still_reads_feeds() -> None:
    """A platform claims by host, so asking for it unasked would turn a
    graph crawl that reaches the platform into a feed crawl.  A feed
    claims by the document's root element and cannot mistake anything,
    so a crawl that reaches one should read it whatever it started as.
    """
    adapters = _build_harvester(Settings(), Canonicalizer())._adapters
    assert [a.PLATFORM for a in adapters] == ["rss"]


def test_an_unclaimed_page_is_read_as_a_page(tmp_path: Path) -> None:
    """A crawl wanders off: an analyzer endorses the shop's own site.

    The adapter is not consulted there, so a site that happens to use the
    platform's path shape cannot hand back candidates pointing at the
    wrong host. What it does hand back is its own links, resolved against
    its own base -- which is what a link graph reads, and what the older
    "not my domain, return nothing" branch silently threw away.
    """
    trap = b"""<html><body>
    <a href="/p/AAA111/">looks like a post</a>
    <a href="/p/BBB222/">so does this</a>
    </body></html>"""
    page = _page(tmp_path, trap, "https://mollyteaca.com/promotions")
    out = PageHarvester(Canonicalizer(), [instagram]).harvest(page, depth=0)
    assert not out.listing, "nobody claimed it, so it was never judged as a listing"
    assert [c.url.canonical for c in out.candidates] == [
        "https://mollyteaca.com/p/AAA111/",
        "https://mollyteaca.com/p/BBB222/",
    ]


def test_platform_check_reads_the_adapter(tmp_path: Path) -> None:
    """Swapping the adapter swaps which pages are ours, with no edit here."""

    class _Elsewhere:
        PLATFORM = "elsewhere"
        DOMAIN = "elsewhere.example"

        problem = staticmethod(instagram.problem)
        parse_item = staticmethod(instagram.parse_item)
        parse_listing = staticmethod(instagram.parse_listing)

        claims = staticmethod(lambda page, document: page.url.reg_domain == "elsewhere.example")

    page = _page(tmp_path, _LISTING, "https://www.instagram.com/mollytea_canada/")
    out = PageHarvester(Canonicalizer(), [_Elsewhere()]).harvest(page, depth=0)
    assert not out.listing, "this adapter does not claim instagram, so nothing parsed it as a feed"


def test_a_listing_says_it_was_one(tmp_path: Path) -> None:
    """Only a listing can be judged empty, so only a listing says so."""
    page = _page(tmp_path, _LISTING, "https://www.instagram.com/mollytea_canada/")
    assert PageHarvester(Canonicalizer(), [instagram]).harvest(page, depth=0).listing


def test_a_post_is_not_a_listing(tmp_path: Path) -> None:
    """An item yields nothing by design and must never read as broken."""
    page = _page(tmp_path, _POST, "https://www.instagram.com/p/AAA111/")
    out = PageHarvester(Canonicalizer(), [instagram]).harvest(page, depth=0)
    assert out.candidates == []
    assert not out.listing


def test_a_link_graph_page_is_not_a_listing(tmp_path: Path) -> None:
    """A page with no links is ordinary; the graph has no listings."""
    page = _page(tmp_path, b"<html><body>no links here</body></html>", "https://example.com/")
    out = PageHarvester(Canonicalizer()).harvest(page, depth=0)
    assert out.candidates == []
    assert not out.listing
