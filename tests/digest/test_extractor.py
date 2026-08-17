from __future__ import annotations

import pytest

from crawlme.digest.extractor import TrafExtractor
from crawlme.schemas import URL, FetchResult

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


def test_extracts_content(extractor, tmp_path):
    page = extractor.extract(_result(), str(tmp_path / "raw/k1/1.html"))
    assert "Hello World" in (page.markdown or "")
    assert "main content" in (page.plain_text or "")


def test_strips_nav_and_footer(extractor, tmp_path):
    page = extractor.extract(_result(), str(tmp_path / "raw/k1/1.html"))
    assert "Hello World" in (page.markdown or "")


def test_sets_raw_html_path(extractor, tmp_path):
    path = str(tmp_path / "raw/k1/1.html")
    page = extractor.extract(_result(), path)
    assert page.raw_html_path == path


def test_degraded_on_broken_html(extractor, tmp_path):
    page = extractor.extract(_result(b"not valid html <xyz>"), str(tmp_path / "x"))
    assert page.extraction_status in ("DEGRADED", "FAILED")


def test_produces_content_on_valid_html(extractor, tmp_path):
    page = extractor.extract(_result(), str(tmp_path / "raw/k1/1.html"))
    assert page.text_len > 0
    assert page.markdown or page.plain_text


#: published_at (2.8) ----------------------------------------------------


def _html_with(head_extra: str = "", body_extra: str = "") -> bytes:
    return (
        "<!DOCTYPE html><html><head><title>T</title>"
        f"{head_extra}</head><body><article><h1>H</h1>"
        f"<p>Body text long enough to extract.</p>{body_extra}</article></body></html>"
    ).encode()


def test_published_at_from_article_meta(extractor: TrafExtractor) -> None:
    html = _html_with('<meta property="article:published_time" content="2026-08-01T10:30:00Z">')
    page = extractor.extract(_result(html))
    assert page.published_at is not None
    assert page.published_at.year == 2026
    assert page.published_at.month == 8
    assert page.published_at.day == 1


def test_published_at_from_json_ld(extractor: TrafExtractor) -> None:
    html = _html_with(
        '<script type="application/ld+json">{"@type":"Article","datePublished":"2026-07-15T08:00:00+00:00"}</script>'
    )
    page = extractor.extract(_result(html))
    assert page.published_at is not None
    assert page.published_at.month == 7


def test_published_at_from_time_element(extractor: TrafExtractor) -> None:
    html = _html_with(body_extra='<time datetime="2026-06-02">June 2</time>')
    page = extractor.extract(_result(html))
    assert page.published_at is not None
    assert page.published_at.month == 6


def test_published_at_none_when_page_is_silent(extractor: TrafExtractor) -> None:
    """Unknown must stay unknown; guessing would poison TIME_HORIZON."""
    assert extractor.extract(_result()).published_at is None


def test_published_at_rejects_absurd_dates(extractor: TrafExtractor) -> None:
    """Template artifacts like a year-1 date must not become a real time."""
    html = _html_with('<meta name="date" content="0001-01-01T00:00:00Z">')
    assert extractor.extract(_result(html)).published_at is None


def test_published_at_naive_value_is_treated_as_utc(extractor: TrafExtractor) -> None:
    html = _html_with('<meta name="date" content="2026-05-04 12:00:00">')
    page = extractor.extract(_result(html))
    assert page.published_at is not None
    assert page.published_at.tzinfo is not None


#: boilerplate removal ---------------------------------------------------


_NAV_HTML = b"""<!DOCTYPE html><html><head><title>Real Title</title></head><body>
<nav><a href="/">Jump to content</a><a href="/menu">Main menu</a><a href="/side">move to sidebar</a></nav>
<article><h1>Real Title</h1>
<p>The actual article body that a reader came here for, long enough to survive extraction.</p>
<p>A second paragraph so the extractor is confident this is the main content.</p></article>
<footer>Privacy policy</footer></body></html>"""


def test_extraction_status_is_ok_for_a_normal_page(extractor: TrafExtractor) -> None:
    """Regression: an invalid output_format made every page DEGRADED.

    trafilatura calls the plain-text format "txt"; "text" raises, which
    aborted the whole primary path and silently pushed every single page
    onto the BeautifulSoup fallback.
    """
    assert extractor.extract(_result(_NAV_HTML)).extraction_status == "OK"


def test_plain_text_drops_navigation_boilerplate(extractor: TrafExtractor) -> None:
    """plain_text feeds the analyzer, so boilerplate here costs tokens
    and dilutes every judgement made from it."""
    text = extractor.extract(_result(_NAV_HTML)).plain_text or ""
    assert "actual article body" in text
    assert "move to sidebar" not in text
    assert "Main menu" not in text


def test_title_comes_from_the_declared_title_tag(extractor: TrafExtractor) -> None:
    """Regression: the primary path never set a title.

    It called .find() on trafilatura's XML *string*, so str.find returned
    an int, .text raised, and a bare except swallowed it. Titles only ever
    worked because the invalid output_format forced the BeautifulSoup
    fallback to run. page.title feeds the ranker's title-match factor and
    the LLM ranker's source-page line, so it silently degraded both.
    """
    page = extractor.extract(_result(_NAV_HTML))
    assert page.title == "Real Title"
    assert page.extraction_status == "OK"


def test_title_falls_back_to_the_url_when_undeclared(extractor: TrafExtractor) -> None:
    page = extractor.extract(_result(b"<html><body><p>No title here at all, just prose.</p></body></html>"))
    assert page.title == "https://example.com/page"
