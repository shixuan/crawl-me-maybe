"""PlaywrightFetcher: session loading, wiring, and one real browser run.

Most of this needs no browser. The one test that does is marked `browser`
so CI skips it, for the same reason CI skips `e2e`: it depends on a
chromium install and its system libraries, which is not a property of the
commit under test.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from crawlme.config import Settings
from crawlme.digest.fetcher import FetchError, HttpFetcher, PlaywrightFetcher
from crawlme.digest.fetcher.browser import _load_storage_state
from crawlme.scheduler.factory import _build_fetcher
from crawlme.schemas import URL, FrontierItem


def _item(url: str) -> FrontierItem:
    return FrontierItem(url=URL(raw=url, canonical=url, url_key="k1"), url_key="k1")


#: session file ----------------------------------------------------------


def test_storage_state_loads(tmp_path: Path) -> None:
    p = tmp_path / "session.json"
    p.write_text(json.dumps({"cookies": [{"name": "sessionid", "value": "x"}], "origins": []}))
    assert _load_storage_state(str(p))["cookies"][0]["name"] == "sessionid"


def test_storage_state_missing_file_fails_loudly(tmp_path: Path) -> None:
    """Silently falling back to anonymous would crawl a login wall.

    On a login-walled platform that means a few hundred fetches of the
    same sign-in page and a conclusion that the site is empty.
    """
    with pytest.raises(FetchError, match="not found"):
        _load_storage_state(str(tmp_path / "nope.json"))


def test_storage_state_bad_json_fails_loudly(tmp_path: Path) -> None:
    p = tmp_path / "session.json"
    p.write_text("not json at all")
    with pytest.raises(FetchError, match="readable JSON"):
        _load_storage_state(str(p))


def test_storage_state_without_cookies_fails_loudly(tmp_path: Path) -> None:
    p = tmp_path / "session.json"
    p.write_text(json.dumps({"cookies": [], "origins": []}))
    with pytest.raises(FetchError, match="no cookies"):
        _load_storage_state(str(p))


#: factory wiring --------------------------------------------------------


def test_factory_builds_http_fetcher_by_default() -> None:
    assert isinstance(_build_fetcher(Settings()), HttpFetcher)


def test_factory_builds_browser_fetcher_when_asked() -> None:
    assert isinstance(_build_fetcher(Settings(fetcher="browser")), PlaywrightFetcher)


def test_constructing_the_browser_fetcher_starts_nothing() -> None:
    """Building a scheduler must not launch a browser."""
    f = PlaywrightFetcher(storage_state="/nonexistent/session.json")
    assert f._context is None
    assert f._browser is None


@pytest.mark.asyncio
async def test_aclose_before_any_fetch_is_a_noop() -> None:
    await PlaywrightFetcher().aclose()


#: one real browser ------------------------------------------------------

_JS_PAGE = b"""<html><head><title>Feed</title></head><body>
<div id="posts">loading...</div>
<script>
  setTimeout(function () {
    document.getElementById('posts').innerHTML = '<article>free bubble tea today</article>';
  }, 200);
</script></body></html>"""


# The shape a feed listing has: a grid built from an API answer the DOM
# never shows. "free tea for members" exists only in the payload.
_XHR_PAGE = b"""<html><head><title>Grid</title></head><body>
<div id="grid">loading...</div>
<script>
  fetch('/api/posts').then(r => r.json()).then(function (d) {
    document.getElementById('grid').innerHTML =
      d.items.map(function (i) { return '<a href="/p/' + i.code + '/"></a>'; }).join('');
  });
</script></body></html>"""

_XHR_DATA = b'{"items": [{"code": "AAA111", "caption": "free tea for members"}]}'


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body, ctype = (_XHR_DATA, "application/json") if self.path.startswith("/api/") else (_JS_PAGE, "text/html")
        if self.path == "/grid":
            body, ctype = _XHR_PAGE, "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence the access log."""


@pytest.fixture
def js_site() -> object:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_renders_javascript_and_reuses_the_browser(js_site: str) -> None:
    """The reason this fetcher exists: content that HTTP alone cannot see."""
    pytest.importorskip("playwright")
    fetcher = PlaywrightFetcher()
    try:
        first = await fetcher.fetch(_item(js_site))
        html = first.raw.decode()
        assert first.status_code == 200
        assert "free bubble tea today" in html, "JS-built content never rendered"
        assert "loading..." not in html, "returned the pre-render shell"

        context = fetcher._context
        await fetcher.fetch(_item(js_site))
        assert fetcher._context is context, "browser was rebuilt between fetches"
    finally:
        await fetcher.aclose()
        await fetcher.aclose()


@pytest.mark.asyncio
async def test_a_transient_navigation_failure_is_retried(monkeypatch):
    """Regression: the browser fetcher had no retry at all.

    Rendering fails transiently more often than an HTTP GET does, so the
    fetcher that needed retries most was the one without them.
    """
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _d: real_sleep(0))
    fetcher = PlaywrightFetcher(max_retries=3)
    calls = []

    class _NavigationTimeoutError(Exception):
        """Named for the classifier, which matches on "Timeout"."""

    async def flaky(item):
        calls.append(len(calls) + 1)
        if len(calls) < 3:
            raise _NavigationTimeoutError("navigation timed out")
        return "ok"

    monkeypatch.setattr(fetcher, "_attempt", flaky)
    assert await fetcher.fetch(_item("https://x.com/a")) == "ok"
    assert calls == [1, 2, 3]


@pytest.mark.asyncio
async def test_a_permanent_browser_error_is_not_retried(monkeypatch):
    fetcher = PlaywrightFetcher(max_retries=3)
    calls = []

    async def gone(item):
        calls.append(1)
        raise FetchError("Permanent HTTP error: 404")

    monkeypatch.setattr(fetcher, "_attempt", gone)
    with pytest.raises(FetchError):
        await fetcher.fetch(_item("https://x.com/a"))
    assert calls == [1], "a 404 does not become a 200 by asking again"


#: what the page fetched for itself --------------------------------------


@pytest.mark.browser
@pytest.mark.asyncio
async def test_keeps_the_answer_the_page_built_itself_from(js_site: str) -> None:
    """The text a listing shows nobody still arrives over the wire.

    Keeping it costs no request: the page already asked for it, and only
    the choice to drop it stood between the crawl and the content.
    """
    pytest.importorskip("playwright")
    fetcher = PlaywrightFetcher(keep_payload=lambda url, ctype: "json" in ctype)
    try:
        result = await fetcher.fetch(_item(js_site + "grid"))
        assert "free tea for members" not in result.raw.decode(), "the DOM never shows it"
        bodies = b"".join(p.body for p in result.payloads)
        assert b"free tea for members" in bodies, "the payload was dropped"
        assert all("/api/" in p.url for p in result.payloads)
    finally:
        await fetcher.aclose()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_nothing_is_kept_unless_something_asks(js_site: str) -> None:
    """A link-graph crawl has no use for it and must pay nothing."""
    pytest.importorskip("playwright")
    fetcher = PlaywrightFetcher()
    try:
        result = await fetcher.fetch(_item(js_site + "grid"))
        assert result.payloads == []
    finally:
        await fetcher.aclose()


@pytest.mark.asyncio
async def test_payloads_stop_at_the_size_cap() -> None:
    """One page can download tens of megabytes; memory is not free."""

    class _Resp:
        url = "https://x/api"
        headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}

        async def body(self) -> bytes:
            return b"x" * 800

    fetcher = PlaywrightFetcher(keep_payload=lambda u, c: True, max_payload_bytes=1000)
    kept: list = []
    await fetcher._read_body(_Resp(), "application/json", kept)
    await fetcher._read_body(_Resp(), "application/json", kept)
    assert len(kept) == 1, "the second body would have crossed the cap"


@pytest.mark.asyncio
async def test_a_body_that_is_gone_is_not_an_error() -> None:
    """A missing payload is a weaker crawl, never a failed one."""

    class _Gone:
        url = "https://x/api"

        async def body(self) -> bytes:
            raise RuntimeError("body already discarded")

    fetcher = PlaywrightFetcher(keep_payload=lambda u, c: True)
    kept: list = []
    await fetcher._read_body(_Gone(), "application/json", kept)
    assert kept == []
