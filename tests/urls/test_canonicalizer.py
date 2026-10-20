from __future__ import annotations

import pytest

from crawlme.urls import Canonicalizer


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM:443//a//b?utm_source=fb&b=2&a=1&fbclid=123#section", "https://example.com/a/b?a=1&b=2"),
        ("/page?x=1", "https://base.com/page?x=1"),
        ("https://x.com/a?", "https://x.com/a"),
    ],
)
def test_normalizes_url(raw, expected):
    url = Canonicalizer().canonicalize(raw, "https://base.com/blog/")
    assert url.canonical == expected
    assert url.raw == raw


def test_identity_ignores_tracking():
    canonicalizer = Canonicalizer()
    plain, tracked, other = [
        canonicalizer.canonicalize(value, "https://x.com")
        for value in ("https://x.com/a", "https://x.com/a?utm_source=fb", "https://x.com/b")
    ]
    assert plain.url_key == tracked.url_key
    assert plain.url_key != other.url_key


@pytest.mark.parametrize("host", ["www.example.com", "sub.example.com"])
def test_domain_scope(host):
    url = Canonicalizer().canonicalize(f"https://{host}/page?key=val", "https://x.com")
    assert url.host == url.domain == host
    assert url.reg_domain == host.removeprefix("www.")
    assert (url.scheme, url.path, url.query) == ("https", "/page", "key=val")
