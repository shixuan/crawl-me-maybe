from __future__ import annotations

import datetime

import pytest

from crawlme.pioneer.prefilter import Decision, PreFilter, PreFilterContext
from crawlme.schemas import URL, Candidate, CrawlGoal


def _url(
    raw: str = "https://example.com/page",
    url_key: str = "k1",
    reg_domain: str = "example.com",
    path: str = "/page",
) -> URL:
    return URL(raw=raw, canonical=raw, url_key=url_key, reg_domain=reg_domain, path=path)


def _candidate(url: URL | None = None, **kw) -> Candidate:
    u = url or _url()
    return Candidate(url=u, **kw)


def _ctx(**kw) -> PreFilterContext:
    return PreFilterContext(**kw)


@pytest.fixture
def pf() -> PreFilter:
    return PreFilter()


def _drop(pf, c, ctx) -> str:
    decision, reason = pf.check(c, CrawlGoal(prompt="test"), ctx)
    assert decision == Decision.DROP
    return reason


def _allow(pf, c, ctx) -> None:
    decision, _ = pf.check(c, CrawlGoal(prompt="test"), ctx)
    assert decision == Decision.ALLOW


# -- scope ------------------------------------------------------------------


def test_drop_outside_scope(pf):
    ctx = _ctx(allowed_domains={"github.com"})
    u = _url(reg_domain="example.com")
    assert _drop(pf, _candidate(url=u), ctx) == "scope"


def test_allow_inside_scope(pf):
    ctx = _ctx(allowed_domains={"example.com"})
    _allow(pf, _candidate(), ctx)


# -- dedup ------------------------------------------------------------------


def test_drop_visited(pf):
    ctx = _ctx(visited={"k1"})
    assert _drop(pf, _candidate(), ctx) == "dedup"


def test_drop_frontier(pf):
    ctx = _ctx(frontier_keys={"k1"})
    assert _drop(pf, _candidate(), ctx) == "dedup"


# -- robots -----------------------------------------------------------------


def test_drop_disallowed(pf):
    ctx = _ctx(allow_fetch=lambda url: False)
    assert _drop(pf, _candidate(), ctx) == "robots"


def test_allow_when_no_policy(pf):
    _allow(pf, _candidate(), _ctx())


# -- protocol ---------------------------------------------------------------


def test_drop_javascript(pf):
    u = _url(raw="javascript:void(0)")
    assert _drop(pf, _candidate(url=u), _ctx()) == "protocol"


def test_drop_mailto(pf):
    u = _url(raw="mailto:a@b.com")
    assert _drop(pf, _candidate(url=u), _ctx()) == "protocol"


# -- extension --------------------------------------------------------------


def test_drop_jpg(pf):
    u = _url(raw="https://x.com/photo.jpg", url_key="k_jpg")
    assert _drop(pf, _candidate(url=u, depth=1), _ctx()) == "extension"


def test_drop_pdf(pf):
    u = _url(raw="https://x.com/doc.pdf", url_key="k_pdf")
    assert _drop(pf, _candidate(url=u, depth=1), _ctx()) == "extension"


def test_allow_html(pf):
    u = _url(raw="https://x.com/page.html", url_key="k_html")
    _allow(pf, _candidate(url=u), _ctx())


# -- url pattern ------------------------------------------------------------


def test_drop_login(pf):
    u = _url(raw="https://x.com/login/", path="/login/", url_key="k2")
    assert _drop(pf, _candidate(url=u), _ctx()) == "url_pattern"


def test_drop_cart(pf):
    u = _url(raw="https://x.com/cart/", path="/cart/", url_key="k3")
    assert _drop(pf, _candidate(url=u), _ctx()) == "url_pattern"


# -- depth ------------------------------------------------------------------


def test_drop_too_deep(pf):
    c = _candidate(depth=6)
    ctx = _ctx()
    goal = CrawlGoal(prompt="test", depth_limit=5)
    decision, reason = pf.check(c, goal, ctx)
    assert decision == Decision.DROP
    assert "depth" in reason


# -- domain budget ----------------------------------------------------------


def test_drop_exhausted(pf):
    ctx = _ctx(domain_counters={"example.com": 50})
    goal = CrawlGoal(prompt="test", domain_budget=50)
    decision, reason = pf.check(_candidate(), goal, ctx)
    assert decision == Decision.DROP
    assert "domain_budget" in reason


def test_allow_under_budget(pf):
    ctx = _ctx(domain_counters={"example.com": 30})
    _allow(pf, _candidate(), ctx)


# time window -----------------------------------------------------------


def _dated(posted_at: datetime.datetime | None) -> Candidate:
    return Candidate(
        url=URL(raw="https://www.instagram.com/a/p/X/", canonical="https://www.instagram.com/a/p/X/", url_key="x"),
        depth=1,
        posted_at=posted_at,
    )


def test_stale_candidate_dropped():
    """The saving that makes a funnel worth having on a feed."""
    goal = CrawlGoal(prompt="test", since=datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc))
    stale = datetime.datetime(2026, 7, 20, tzinfo=datetime.timezone.utc)
    decision, rule = PreFilter().check(_dated(stale), goal, PreFilterContext())
    assert (decision, rule) == (Decision.DROP, "stale")


def test_undated_candidate_kept():
    """Unknown is not old. Platforms omit the date often enough to matter."""
    goal = CrawlGoal(prompt="test", since=datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc))
    assert PreFilter().check(_dated(None), goal, PreFilterContext())[0] is Decision.ALLOW


def test_window_off_without_a_goal_setting():
    assert (
        PreFilter().check(_dated("2020-01-01T00:00:00+00:00"), CrawlGoal(prompt="test"), PreFilterContext())[0]
        is Decision.ALLOW
    )


def test_zero_domain_budget_means_no_ceiling():
    """Every post on a platform shares its domain, so a per-domain cap
    becomes a total one wearing the wrong name."""
    goal = CrawlGoal(prompt="test", domain_budget=0)
    ctx = PreFilterContext(domain_counters={"instagram.com": 500})
    c = Candidate(
        url=URL(
            raw="https://www.instagram.com/a/p/X/",
            canonical="https://www.instagram.com/a/p/X/",
            url_key="x",
            reg_domain="instagram.com",
        ),
        depth=1,
    )
    assert PreFilter().check(c, goal, ctx)[0] is Decision.ALLOW


# -- a seed is not wandering -------------------------------------------------


def test_a_seed_is_not_judged_by_its_extension(pf):
    """Feeds are named exactly what this list refuses.

    A run seeded with `feed.xml` lost it silently before anything was
    fetched: four feeds given, three ingested, and the reason only ever
    logged at DEBUG.
    """
    for raw in ("https://blog.rust-lang.org/feed.xml", "https://news.ycombinator.com/rss"):
        _allow(pf, _candidate(url=_url(raw=raw, url_key=raw)), _ctx())


def test_a_discovered_link_still_is(pf):
    """The list guards against wandering into archives and documents,
    which is what a link found on a page might be."""
    u = _url(raw="https://x.com/manual.pdf", url_key="k_pdf2")
    assert _drop(pf, _candidate(url=u, depth=1), _ctx()) == "extension"
