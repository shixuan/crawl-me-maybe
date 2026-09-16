from __future__ import annotations

import hashlib
import json

import pytest

from crawlme.digest.extractor import TrafExtractor
from crawlme.platforms import instagram, reddit, rss
from crawlme.schemas import URL, FetchResult, Payload


def _ig_result():
    result = _result()
    result.url = URL(
        raw="https://www.instagram.com/shop/p/PARENT/",
        canonical="https://www.instagram.com/shop/p/PARENT/",
        url_key="k1",
    )
    result.payloads = [
        Payload(
            body=json.dumps(
                [
                    {"code": "OTHER", "caption": {"text": "Unrelated recommendation"}},
                    {
                        "code": "PARENT",
                        "caption": {"text": "Buy two drinks."},
                        "carousel_media": [
                            {"code": "CHILD", "caption": {"text": "Free lantern."}},
                            {"code": "CHILD2", "caption": {"text": "Buy two drinks."}},
                        ],
                    },
                ]
            ).encode()
        )
    ]
    return result


def test_adapter_page_text(monkeypatch):
    import crawlme.digest.extractor as module

    def unexpected(*args, **kwargs):
        pytest.fail("HTML extraction must not replace matched payload text")

    monkeypatch.setattr(module.trafilatura, "extract", unexpected)
    page = TrafExtractor(adapters=[instagram]).extract(_ig_result())
    text = "Buy two drinks.\n\nFree lantern."
    assert page.plain_text == page.markdown == text
    assert page.text_len == len(text)
    assert page.text_hash == hashlib.sha256(text.encode()).hexdigest()[:16]
    assert page.metadata["text_source"] == "instagram"
    assert page.title == "Test Page"
    assert page.extraction_status == "OK"


def test_inline_target_with_recommendations():
    result = _ig_result()
    data = json.loads(result.payloads[0].body)
    result.raw += ('<script type="application/json">' + json.dumps(data[1]) + "</script>").encode()
    result.payloads = [Payload(body=json.dumps([data[0]]).encode())]
    page = TrafExtractor(adapters=[instagram]).extract(result)
    assert page.plain_text == "Buy two drinks.\n\nFree lantern."
    assert page.metadata["text_source"] == "instagram"


@pytest.mark.parametrize("case", ["missing", "malformed", "unmatched", "listing", "redirect", "login", "http_error"])
def test_adapter_text_fallback(case):
    result = _ig_result()
    if case == "missing":
        result.payloads = []
    elif case == "malformed":
        result.payloads = [Payload(body=b"not json")]
    elif case == "unmatched":
        result.url.canonical = "https://www.instagram.com/shop/p/UNKNOWN/"
    elif case == "listing":
        result.url.canonical = "https://www.instagram.com/shop/"
    elif case == "redirect":
        result.final_url = URL(
            raw="https://www.instagram.com/p/OTHER/", canonical="https://www.instagram.com/p/OTHER/", url_key="other"
        )
    elif case == "login":
        result.raw += b'<form id="loginform"></form>'
    else:
        result.status_code = 404
    expected = TrafExtractor().extract(result)
    actual = TrafExtractor(adapters=[instagram]).extract(result)
    assert actual.plain_text == expected.plain_text
    assert actual.markdown == expected.markdown
    assert "text_source" not in actual.metadata


@pytest.mark.parametrize(
    "url",
    ["https://example.com/page", "https://www.reddit.com/r/test/comments/abc/post", "https://example.com/feed.xml"],
)
def test_other_platform_text(url):
    result = _result()
    result.url.canonical = url
    expected = TrafExtractor().extract(result)
    actual = TrafExtractor(adapters=[instagram, reddit, rss]).extract(result)
    assert actual.plain_text == expected.plain_text
    assert actual.markdown == expected.markdown


SAMPLE_HTML = b"""<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
    <nav><a href="/">Home</a></nav>
    <article>
        <h1>Hello World</h1>
        <p>This is the main content of the page.</p>
        <p>It has multiple paragraphs.</p>
    </article>
    <footer>Copyright 2024</footer>
</body>
</html>"""


def _result(html: bytes = SAMPLE_HTML, status: int = 200) -> FetchResult:
    url = URL(raw="https://example.com/page", canonical="https://example.com/page", url_key="k1")
    return FetchResult(item_id="i1", url_key="k1", url=url, status_code=status, raw=html)


@pytest.fixture
def extractor() -> TrafExtractor:
    return TrafExtractor()


def test_extracts_title(extractor, tmp_path):
    page = extractor.extract(_result(), str(tmp_path / "raw/k1/1.html"))
    assert page.title == "Test Page"


def test_extracts(extractor, tmp_path):
    page = extractor.extract(_result(), str(tmp_path / "raw/k1/1.html"))
    assert "Hello World" in (page.markdown or "")
    assert "main content" in (page.plain_text or "")


def test_strips_chrome(extractor, tmp_path):
    page = extractor.extract(_result(), str(tmp_path / "raw/k1/1.html"))
    assert "Hello World" in (page.markdown or "")


def test_raw_path(extractor, tmp_path):
    path = str(tmp_path / "raw/k1/1.html")
    page = extractor.extract(_result(), path)
    assert page.raw_html_path == path


def test_broken_degrades(extractor, tmp_path):
    page = extractor.extract(_result(b"not valid html <xyz>"), str(tmp_path / "x"))
    assert page.extraction_status in ("DEGRADED", "FAILED")


def test_valid_content(extractor, tmp_path):
    page = extractor.extract(_result(), str(tmp_path / "raw/k1/1.html"))
    assert page.text_len > 0
    assert page.markdown or page.plain_text


# published_at (2.8) ----------------------------------------------------


def _html_with(head_extra: str = "", body_extra: str = "") -> bytes:
    return (
        "<!DOCTYPE html><html><head><title>T</title>"
        f"{head_extra}</head><body><article><h1>H</h1>"
        f"<p>Body text long enough to extract.</p>{body_extra}</article></body></html>"
    ).encode()


_JSON_LD = '<script type="application/ld+json">{"@type":"Article","datePublished":"2026-07-15T08:00:00+00:00"}</script>'


@pytest.mark.parametrize(
    ("markup", "in_body", "expected_month"),
    [
        ('<meta property="article:published_time" content="2026-08-01T10:30:00Z">', False, 8),
        (_JSON_LD, False, 7),
        ('<time datetime="2026-06-02">June 2</time>', True, 6),
        # Unknown must stay unknown; guessing would poison the stale streak.
        ("", False, None),
        # A template artifact like a year-1 date is not a real time.
        ('<meta name="date" content="0001-01-01T00:00:00Z">', False, None),
    ],
)
def test_published_at(extractor: TrafExtractor, markup, in_body, expected_month) -> None:
    html = _html_with(body_extra=markup) if in_body else _html_with(markup)
    page = extractor.extract(_result(html))
    assert (page.published_at.month if page.published_at else None) == expected_month


def test_naive_is_utc(extractor: TrafExtractor) -> None:
    html = _html_with('<meta name="date" content="2026-05-04 12:00:00">')
    page = extractor.extract(_result(html))
    assert page.published_at is not None
    assert page.published_at.tzinfo is not None


# boilerplate removal ---------------------------------------------------


_NAV_HTML = b"""<!DOCTYPE html><html><head><title>Real Title</title></head><body>
<nav><a href="/">Jump to content</a><a href="/menu">Main menu</a><a href="/side">move to sidebar</a></nav>
<article><h1>Real Title</h1>
<p>The actual article body that a reader came here for, long enough to survive extraction.</p>
<p>A second paragraph so the extractor is confident this is the main content.</p></article>
<footer>Privacy policy</footer></body></html>"""


def test_status_ok(extractor: TrafExtractor) -> None:
    """Regression: an invalid output_format made every page DEGRADED.

    trafilatura calls the plain-text format "txt"; "text" raises, which
    aborted the whole primary path and silently pushed every single page
    onto the BeautifulSoup fallback.
    """
    assert extractor.extract(_result(_NAV_HTML)).extraction_status == "OK"


def test_text_no_chrome(extractor: TrafExtractor) -> None:
    """plain_text feeds the analyzer, so boilerplate here costs tokens
    and dilutes every judgement made from it."""
    text = extractor.extract(_result(_NAV_HTML)).plain_text or ""
    assert "actual article body" in text
    assert "move to sidebar" not in text
    assert "Main menu" not in text


def test_title_tag(extractor: TrafExtractor) -> None:
    """Read the HTML title on the primary extraction path, not only during fallback."""
    page = extractor.extract(_result(_NAV_HTML))
    assert page.title == "Real Title"
    assert page.extraction_status == "OK"


def test_title_from_url(extractor: TrafExtractor) -> None:
    page = extractor.extract(_result(b"<html><body><p>No title here at all, just prose.</p></body></html>"))
    assert page.title == "https://example.com/page"
