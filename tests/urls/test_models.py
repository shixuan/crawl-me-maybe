from __future__ import annotations

from crawlme.urls import URL, RawLink


def test_url_defaults():
    u = URL(raw="r", canonical="c", url_key="k")
    assert (u.scheme, u.host, u.reg_domain) == ("", "", "")


def test_link_defaults():
    r = RawLink(href="/page")
    assert r.anchor is None
    assert r.position == 0
