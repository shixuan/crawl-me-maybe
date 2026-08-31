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
from unittest.mock import AsyncMock

import pytest

from crawlme.config import Settings
from crawlme.digest.fetcher import DispatchingFetcher, FetchError, PlaywrightFetcher
from crawlme.digest.fetcher.browser import _load_storage_state
from crawlme.scheduler.factory import _build_fetcher
from crawlme.schemas import URL, FrontierItem


def _item(url: str) -> FrontierItem:
    return FrontierItem(url=URL(raw=url, canonical=url, url_key="k1"), url_key="k1")


# session file ----------------------------------------------------------


def test_state_loads(tmp_path: Path) -> None:
    p = tmp_path / "session.json"
    p.write_text(json.dumps({"cookies": [{"name": "sessionid", "value": "x"}], "origins": []}))
    assert _load_storage_state(str(p))["cookies"][0]["name"] == "sessionid"


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (None, "not found"),
        ("not json at all", "readable JSON"),
        (json.dumps({"cookies": [], "origins": []}), "no cookies"),
    ],
)
def test_state_bad_path(tmp_path: Path, content, match) -> None:
    """Silently falling back to anonymous would crawl a login wall.

    On a login-walled platform that means a few hundred fetches of the
    same sign-in page and a conclusion that the site is empty.
    """
    p = tmp_path / "session.json"
    if content is not None:
        p.write_text(content)
    with pytest.raises(FetchError, match=match):
        _load_storage_state(str(p))


# factory wiring --------------------------------------------------------


def test_factory_default() -> None:
    """Neither answer is right for a whole run: most pages want plain
    HTTP and a few platforms cannot be read without a browser."""
    assert isinstance(_build_fetcher(Settings()), DispatchingFetcher)


def test_factory_browser() -> None:
    """Asking for one is still a way to get one everywhere.  A page
    that is not a platform can need a script run to say anything, and
    only the person crawling it knows that."""
    assert isinstance(_build_fetcher(Settings(fetcher="browser")), PlaywrightFetcher)


def test_session_splits(tmp_path) -> None:
    """A shop the analyser endorsed off a post has no use for the
    platform's cookies, so it takes the cheap route."""
    sess = tmp_path / "s.json"
    sess.write_text('{"cookies": [], "origins": []}')
    built = _build_fetcher(Settings(browser_storage_state=str(sess)))
    assert isinstance(built, DispatchingFetcher)
    # Routing, not install detection: without playwright the dispatcher
    # deliberately degrades, and the bare CI lane has none.
    built._can_render = True
    assert built._browser._storage_state == str(sess)
    assert isinstance(built._pick("https://www.instagram.com/someone/"), PlaywrightFetcher)
    assert not isinstance(built._pick("https://a-shop.example.com/promo"), PlaywrightFetcher)


def test_lazy_launch() -> None:
    """Building a scheduler must not launch a browser."""
    f = PlaywrightFetcher(storage_state="/nonexistent/session.json")
    assert f._context is None
    assert f._browser is None


@pytest.mark.asyncio
async def test_aclose_unused() -> None:
    await PlaywrightFetcher().aclose()


# one real browser ------------------------------------------------------

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
async def test_renders_reuse(js_site: str) -> None:
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
async def test_nav_retried(monkeypatch):
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
async def test_permanent_nav_error(monkeypatch):
    fetcher = PlaywrightFetcher(max_retries=3)
    calls = []

    async def gone(item):
        calls.append(1)
        raise FetchError("Permanent HTTP error: 404")

    monkeypatch.setattr(fetcher, "_attempt", gone)
    with pytest.raises(FetchError):
        await fetcher.fetch(_item("https://x.com/a"))
    assert calls == [1], "a 404 does not become a 200 by asking again"


# what the page fetched for itself --------------------------------------


@pytest.mark.browser
@pytest.mark.asyncio
async def test_keeps_payload(js_site: str) -> None:
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
async def test_no_payload_ask(js_site: str) -> None:
    """A link-graph crawl has no use for it and must pay nothing."""
    pytest.importorskip("playwright")
    fetcher = PlaywrightFetcher()
    try:
        result = await fetcher.fetch(_item(js_site + "grid"))
        assert result.payloads == []
    finally:
        await fetcher.aclose()


@pytest.mark.asyncio
async def test_payload_cap() -> None:
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
async def test_body_gone_ok() -> None:
    """A missing payload is a weaker crawl, never a failed one."""

    class _Gone:
        url = "https://x/api"

        async def body(self) -> bytes:
            raise RuntimeError("body already discarded")

    fetcher = PlaywrightFetcher(keep_payload=lambda u, c: True)
    kept: list = []
    await fetcher._read_body(_Gone(), "application/json", kept)
    assert kept == []


def test_scroll_default() -> None:
    """A link graph has nothing below the fold worth waiting for."""
    assert PlaywrightFetcher()._scrolls == 0


@pytest.mark.asyncio
async def test_scroll_stops() -> None:
    """A short account must not cost every scroll it was allowed."""

    class _Page:
        url = "https://x/"

        def __init__(self) -> None:
            self.wheels = 0
            self.waits = 0

        async def evaluate(self, _js: str) -> int:
            return 1000  # the page never grows

        @property
        def mouse(self):
            return self

        async def wheel(self, _x: int, _y: int) -> None:
            self.wheels += 1

        async def wait_for_timeout(self, _ms: int) -> None:
            self.waits += 1

    page = _Page()
    await PlaywrightFetcher(scrolls=10)._scroll_through(page)
    assert page.wheels == 1, "one scroll, then the height said there was no more"


@pytest.mark.asyncio
async def test_scroll_grows() -> None:
    class _Page:
        url = "https://x/"

        def __init__(self) -> None:
            self.wheels = 0
            self.height = 1000

        async def evaluate(self, _js: str) -> int:
            self.height += 500
            return self.height

        @property
        def mouse(self):
            return self

        async def wheel(self, _x: int, _y: int) -> None:
            self.wheels += 1

        async def wait_for_timeout(self, _ms: int) -> None:
            return None

    page = _Page()
    await PlaywrightFetcher(scrolls=3)._scroll_through(page)
    assert page.wheels == 3


@pytest.mark.asyncio
async def test_one_browser() -> None:
    """The pump pops several seeds at once and they all arrive here.

    Unguarded, every one of them found no context and launched a
    browser; the last assignment won and the rest became processes
    nothing held a reference to, so aclose() could not reach them. One
    run showed five starts where it should have shown one.
    """
    fetcher = PlaywrightFetcher()
    starts = 0

    async def _count_start():
        nonlocal starts
        starts += 1
        await asyncio.sleep(0.01)  # a real launch is slow; that is the window
        fetcher._context = object()  # type: ignore[assignment]
        return fetcher._context

    fetcher._start_context = _count_start  # type: ignore[assignment]
    await asyncio.gather(*(fetcher._ensure_context() for _ in range(5)))
    assert starts == 1


# wait-condition timeouts ------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_keeps(monkeypatch) -> None:
    """networkidle is a condition some pages never reach.

    A platform that polls or throttles keeps a request open forever, so
    the wait times out while the document is sitting there fully
    rendered.  Discarding it threw away a page already paid for, and
    then paid for it twice more on retry.
    """
    # Skipped rather than marked `browser`: this needs the exception
    # class, not a browser, so it runs wherever playwright is installed
    # and stays out of the way where it is not.
    timeout_error = pytest.importorskip("playwright.async_api").TimeoutError

    class _Page:
        url = "https://example.com/p"

        async def goto(self, url, wait_until=None):
            raise timeout_error("Timeout 30000ms exceeded.")

        async def content(self):
            return "<html><body>the post is right here</body></html>"

        async def close(self):
            return None

        def on(self, *_a):
            return None

    class _Context:
        async def new_page(self):
            return _Page()

    fetcher = PlaywrightFetcher()
    monkeypatch.setattr(fetcher, "_ensure_context", AsyncMock(return_value=_Context()))

    result = await fetcher.fetch(_item("https://example.com/p"))

    assert result.status_code == 200
    assert b"the post is right here" in result.raw


@pytest.mark.asyncio
async def test_timeout_empty(monkeypatch) -> None:
    """An empty document means the fetch really did fail."""
    # Skipped rather than marked `browser`: this needs the exception
    # class, not a browser, so it runs wherever playwright is installed
    # and stays out of the way where it is not.
    timeout_error = pytest.importorskip("playwright.async_api").TimeoutError

    class _Page:
        url = "https://example.com/p"

        async def goto(self, url, wait_until=None):
            raise timeout_error("Timeout 30000ms exceeded.")

        async def content(self):
            return "   "

        async def close(self):
            return None

        def on(self, *_a):
            return None

    class _Context:
        async def new_page(self):
            return _Page()

    fetcher = PlaywrightFetcher()
    monkeypatch.setattr(fetcher, "_ensure_context", AsyncMock(return_value=_Context()))

    with pytest.raises(FetchError):
        await fetcher.fetch(_item("https://example.com/p"))
