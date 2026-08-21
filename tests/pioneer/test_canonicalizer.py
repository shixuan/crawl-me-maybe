from __future__ import annotations

import pytest

from crawlme.pioneer.canonicalizer import Canonicalizer


@pytest.fixture
def c() -> Canonicalizer:
    return Canonicalizer()


# -- canonicalize -----------------------------------------------------------


def test_scheme_lower(c):
    url = c.canonicalize("HTTPS://Example.COM/path", "http://x.com")
    assert url.canonical.startswith("https://example.com")


def test_default_port_removed(c):
    url = c.canonicalize("https://example.com:443/a", "http://x.com")
    assert ":443" not in url.canonical


def test_fragment_removed(c):
    url = c.canonicalize("https://x.com/a#section", "http://x.com")
    assert "#" not in url.canonical


def test_tracking_params_removed(c):
    url = c.canonicalize("https://x.com/a?utm_source=fb&b=2", "http://x.com")
    assert "utm_source" not in url.canonical
    assert "b=2" in url.canonical


def test_empty_query_cleared(c):
    url = c.canonicalize("https://x.com/a?", "http://x.com")
    assert "?" not in url.canonical


def test_duplicate_slashes_collapsed(c):
    url = c.canonicalize("https://x.com//a//b", "http://x.com")
    assert "//" not in url.canonical.split("://")[1]


def test_relative_url_resolved(c):
    url = c.canonicalize("/page?x=1", "https://base.com/blog/")
    assert url.canonical == "https://base.com/page?x=1"


def test_url_key_stable(c):
    u1 = c.canonicalize("https://x.com/a", "http://x.com")
    u2 = c.canonicalize("https://x.com/a?utm_source=fb", "http://x.com")
    assert u1.url_key == u2.url_key


def test_url_key_different(c):
    u1 = c.canonicalize("https://x.com/a", "http://x.com")
    u2 = c.canonicalize("https://x.com/b", "http://x.com")
    assert u1.url_key != u2.url_key


def test_javascript_skipped(c):
    url = c.canonicalize("javascript:void(0)", "http://x.com")
    assert url.canonical.startswith("javascript:")


def test_fbclid_removed(c):
    url = c.canonicalize("https://x.com/a?fbclid=123&keep=1", "http://x.com")
    assert "fbclid" not in url.canonical
    assert "keep=1" in url.canonical


def test_query_params_sorted(c):
    url = c.canonicalize("https://x.com/a?b=2&a=1", "http://x.com")
    assert url.canonical == "https://x.com/a?a=1&b=2"


# -- url fields -------------------------------------------------------------


def test_raw_preserved(c):
    url = c.canonicalize("https://X.com/a", "http://x.com")
    assert url.raw == "https://X.com/a"


def test_scheme_populated(c):
    url = c.canonicalize("https://x.com/a", "http://x.com")
    assert url.scheme == "https"


def test_host_populated(c):
    url = c.canonicalize("https://X.com/a", "http://x.com")
    assert url.host == "x.com"


def test_path_populated(c):
    url = c.canonicalize("https://x.com/page/sub", "http://x.com")
    assert "/page/sub" in url.path


def test_query_populated(c):
    url = c.canonicalize("https://x.com/a?key=val", "http://x.com")
    assert url.query == "key=val"


def test_reg_domain_strips_www(c):
    url = c.canonicalize("https://www.example.com/a", "http://x.com")
    assert url.reg_domain == "example.com"


def test_domain_is_host(c):
    url = c.canonicalize("https://sub.example.com/a", "http://x.com")
    assert url.domain == "sub.example.com"
