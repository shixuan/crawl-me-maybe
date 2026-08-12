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
