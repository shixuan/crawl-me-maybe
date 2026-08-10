from __future__ import annotations

import pytest

from crawlme.pioneer.canonicalizer import Canonicalizer


@pytest.fixture
def c() -> Canonicalizer:
    return Canonicalizer()


class TestCanonicalize:
    def test_scheme_lower(self, c):
        url, _ = c.canonicalize("HTTPS://Example.COM/path", "http://x.com")
        assert url.startswith("https://example.com")

    def test_default_port_removed(self, c):
        url, _ = c.canonicalize("https://example.com:443/a", "http://x.com")
        assert ":443" not in url

    def test_fragment_removed(self, c):
        url, _ = c.canonicalize("https://x.com/a#section", "http://x.com")
        assert "#" not in url

    def test_tracking_params_removed(self, c):
        url, _ = c.canonicalize("https://x.com/a?utm_source=fb&b=2", "http://x.com")
        assert "utm_source" not in url
        assert "b=2" in url

    def test_empty_query_cleared(self, c):
        url, _ = c.canonicalize("https://x.com/a?", "http://x.com")
        assert "?" not in url

    def test_duplicate_slashes_collapsed(self, c):
        url, _ = c.canonicalize("https://x.com//a//b", "http://x.com")
        assert "//" not in url.split("://")[1]

    def test_relative_url_resolved(self, c):
        url, _ = c.canonicalize("/page?x=1", "https://base.com/blog/")
        assert url == "https://base.com/page?x=1"

    def test_url_key_stable(self, c):
        _, k1 = c.canonicalize("https://x.com/a", "http://x.com")
        _, k2 = c.canonicalize("https://x.com/a?utm_source=fb", "http://x.com")
        assert k1 == k2

    def test_url_key_different(self, c):
        _, k1 = c.canonicalize("https://x.com/a", "http://x.com")
        _, k2 = c.canonicalize("https://x.com/b", "http://x.com")
        assert k1 != k2

    def test_javascript_skipped(self, c):
        url, _ = c.canonicalize("javascript:void(0)", "http://x.com")
        assert url.startswith("javascript:")

    def test_fbclid_removed(self, c):
        url, _ = c.canonicalize("https://x.com/a?fbclid=123&keep=1", "http://x.com")
        assert "fbclid" not in url
        assert "keep=1" in url

    def test_query_params_sorted(self, c):
        url, _ = c.canonicalize("https://x.com/a?b=2&a=1", "http://x.com")
        assert url == "https://x.com/a?a=1&b=2"
