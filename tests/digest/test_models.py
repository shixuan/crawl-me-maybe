from __future__ import annotations

from crawlme.digest import FetchResult, Page
from crawlme.urls import URL


def make_url() -> URL:
    return URL(
        raw="https://example.com/page?a=1",
        canonical="https://example.com/page",
        url_key="abc123",
    )


def test_fetch_defaults():
    u = make_url()
    fr = FetchResult(item_id="i1", url_key=u.url_key, url=u)
    assert (fr.status_code, fr.raw, fr.fetch_attempt) == (0, b"", 1)


def test_page_defaults():
    u = make_url()
    p = Page(url_key=u.url_key, url=u)
    assert p.page_id
    assert p.extraction_status == "OK"
