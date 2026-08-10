from __future__ import annotations

import pytest

from crawlme.pioneer.prefilter import Decision, PreFilter, PreFilterContext
from crawlme.schemas import Candidate, CrawlGoal, URL


def _url(raw: str = "https://example.com/page", url_key: str = "k1", reg_domain: str = "example.com", path: str = "/page") -> URL:
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
    decision, reason = pf.check(c, CrawlGoal(prompt="test"), ctx)
    assert decision == Decision.ALLOW


class TestScope:
    def test_drop_outside_scope(self, pf):
        ctx = _ctx(allowed_domains={"github.com"})
        u = _url(reg_domain="example.com")
        assert _drop(pf, _candidate(url=u), ctx) == "scope"

    def test_allow_inside_scope(self, pf):
        ctx = _ctx(allowed_domains={"example.com"})
        _allow(pf, _candidate(), ctx)


class TestDedup:
    def test_drop_visited(self, pf):
        ctx = _ctx(visited={"k1"})
        assert _drop(pf, _candidate(), ctx) == "dedup"

    def test_drop_frontier(self, pf):
        ctx = _ctx(frontier_keys={"k1"})
        assert _drop(pf, _candidate(), ctx) == "dedup"


class TestRobots:
    def test_drop_disallowed(self, pf):
        ctx = _ctx(allow_fetch=lambda url: False)
        assert _drop(pf, _candidate(), ctx) == "robots"

    def test_allow_when_no_policy(self, pf):
        _allow(pf, _candidate(), _ctx())


class TestProtocol:
    def test_drop_javascript(self, pf):
        u = _url(raw="javascript:void(0)")
        assert _drop(pf, _candidate(url=u), _ctx()) == "protocol"

    def test_drop_mailto(self, pf):
        u = _url(raw="mailto:a@b.com")
        assert _drop(pf, _candidate(url=u), _ctx()) == "protocol"


class TestExtension:
    def test_drop_jpg(self, pf):
        u = _url(raw="https://x.com/photo.jpg", url_key="k_jpg")
        assert _drop(pf, _candidate(url=u), _ctx()) == "extension"

    def test_drop_pdf(self, pf):
        u = _url(raw="https://x.com/doc.pdf", url_key="k_pdf")
        assert _drop(pf, _candidate(url=u), _ctx()) == "extension"

    def test_allow_html(self, pf):
        u = _url(raw="https://x.com/page.html", url_key="k_html")
        _allow(pf, _candidate(url=u), _ctx())


class TestUrlPattern:
    def test_drop_login(self, pf):
        u = _url(raw="https://x.com/login/", path="/login/", url_key="k2")
        assert _drop(pf, _candidate(url=u), _ctx()) == "url_pattern"

    def test_drop_cart(self, pf):
        u = _url(raw="https://x.com/cart/", path="/cart/", url_key="k3")
        assert _drop(pf, _candidate(url=u), _ctx()) == "url_pattern"


class TestDepth:
    def test_drop_too_deep(self, pf):
        c = _candidate(depth=6)
        ctx = _ctx()
        goal = CrawlGoal(prompt="test", depth_limit=5)
        decision, reason = pf.check(c, goal, ctx)
        assert decision == Decision.DROP
        assert "depth" in reason


class TestDomainBudget:
    def test_drop_exhausted(self, pf):
        ctx = _ctx(domain_counters={"example.com": 50})
        goal = CrawlGoal(prompt="test", domain_budget=50)
        decision, reason = pf.check(_candidate(), goal, ctx)
        assert decision == Decision.DROP
        assert "domain_budget" in reason

    def test_allow_under_budget(self, pf):
        ctx = _ctx(domain_counters={"example.com": 30})
        goal = CrawlGoal(prompt="test", domain_budget=50)
        _allow(pf, _candidate(), ctx)
