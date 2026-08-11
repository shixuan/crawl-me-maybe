from __future__ import annotations

from pathlib import Path

from crawlme.digest.links import extract_links
from crawlme.schemas import URL, Page

PAGE_WITH_LINKS = """<!DOCTYPE html>
<html>
<head><title>Docs</title></head>
<body>
    <nav><a href="/">Home</a></nav>
    <h1>Getting Started</h1>
    <p>Read the <a href="/intro">introduction</a> first.</p>
    <h2>Installation</h2>
    <p>Download from <a href="https://pypi.org">PyPI</a> or clone the repo.</p>
    <h2>API Reference</h2>
    <ul>
        <li><a href="/api/auth">Authentication</a></li>
        <li><a href="/api/data">Data Endpoints</a></li>
    </ul>
</body>
</html>"""

PAGE_NO_LINKS = """<!DOCTYPE html>
<html>
<head><title>Empty</title></head>
<body><p>Nothing to link to.</p></body>
</html>"""

PAGE_HEADING_ANCESTOR = """<!DOCTYPE html>
<html>
<body>
    <h3><a href="/nested">Nested Link</a></h3>
</body>
</html>"""

PAGE_EMPTY_HREF = """<!DOCTYPE html>
<html>
<body>
    <a href="">empty</a>
    <a href="/valid">valid</a>
    <a>no href at all</a>
</body>
</html>"""


def _page(tmp_path: Path, html: str, url_key: str = "k1") -> Page:
    path = tmp_path / f"{url_key}.html"
    path.write_text(html, encoding="utf-8")
    return Page(
        url_key=url_key,
        url=URL(raw="https://example.com", canonical="https://example.com", url_key=url_key),
        raw_html_path=str(path),
    )


def test_extracts_href_and_anchor(tmp_path):
    links = extract_links(_page(tmp_path, PAGE_WITH_LINKS))
    hrefs = {link.href for link in links}
    assert "/intro" in hrefs
    assert "/api/auth" in hrefs
    assert any(link.anchor == "introduction" for link in links)
    assert any(link.anchor == "Authentication" for link in links)


def test_positions_are_sequential(tmp_path):
    links = extract_links(_page(tmp_path, PAGE_WITH_LINKS))
    positions = [link.position for link in links]
    assert positions == sorted(positions)
    assert positions[0] >= 1


def test_snippet_is_parent_text(tmp_path):
    links = extract_links(_page(tmp_path, PAGE_WITH_LINKS))
    auth_link = next(link for link in links if link.anchor == "Authentication")
    assert auth_link.snippet is not None
    assert "Authentication" in auth_link.snippet


def test_parent_heading_from_ancestor(tmp_path):
    links = extract_links(_page(tmp_path, PAGE_HEADING_ANCESTOR))
    assert len(links) == 1
    assert links[0].parent_heading == "Nested Link"


def test_parent_heading_from_preceding(tmp_path):
    links = extract_links(_page(tmp_path, PAGE_WITH_LINKS))
    auth_link = next(link for link in links if link.anchor == "Authentication")
    assert auth_link.parent_heading is not None
    assert "API Reference" in auth_link.parent_heading


def test_skips_links_with_empty_href(tmp_path):
    links = extract_links(_page(tmp_path, PAGE_EMPTY_HREF))
    hrefs = {link.href for link in links}
    assert "" not in hrefs
    assert "/valid" in hrefs


def test_empty_page_returns_empty_list(tmp_path):
    links = extract_links(_page(tmp_path, PAGE_NO_LINKS))
    assert links == []


def test_anchor_none_when_no_text(tmp_path):
    html = '<html><body><a href="/img"><img src="x.png" alt="pic"></a></body></html>'
    links = extract_links(_page(tmp_path, html))
    assert len(links) == 1
    assert links[0].anchor is None
