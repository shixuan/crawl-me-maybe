"""Shared fixtures for integration tests — mock network, real pipeline."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from crawlme.config import Settings

# Minimal HTML page with known link structure for deterministic assertions.
_TEST_HTML = """<!DOCTYPE html>
<html>
<head><title>Test Page Alpha</title></head>
<body>
  <p>This page is about memory safety and compiler design.</p>
  <a href="https://example.com/beta">Memory Safety in Rust</a>
  <a href="https://example.com/gamma">Click here</a>
  <a href="https://example.com/delta">Compiler Design 101</a>
  <a href="https://example.com/epsilon">About Us</a>
  <a href="https://example.com/zeta">Download</a>
  <a href="/pdf/report.pdf">PDF Report</a>
  <a href="javascript:void(0)">JS Link</a>
  <a href="https://wikidata.org/wiki/Q123">Wikidata Entry</a>
  <a href="https://example.com/✋">Emoji Page</a>
</body>
</html>"""


@pytest.fixture
def integration_settings(tmp_path: Path) -> Settings:
    """Settings for integration tests — temp dir, no robots, rule-only.

    Everything pinned explicitly so tests are deterministic regardless
    of the developer's .env.
    """
    return Settings(
        result_dir=tmp_path,
        ignore_robots=True,
        fetch_concurrency=1,
        log_level="INFO",
        log_format="console",
        embedding_provider="",
        embedding_model="",
        embedding_api_key="",
        embedding_base_url="",
    )


class MockTransport(httpx.AsyncBaseTransport):
    """httpx transport that returns our test HTML for every request."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_TEST_HTML.encode(), headers={"Content-Type": "text/html"})


@pytest.fixture
def mock_http_client() -> httpx.AsyncClient:
    """Async httpx client using MockTransport."""
    return httpx.AsyncClient(transport=MockTransport())
