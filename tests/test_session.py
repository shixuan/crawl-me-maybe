from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crawlme.cli.session import SessionError, capture, cmd_session


def _args(**kw) -> argparse.Namespace:
    base = {"path": "s.json", "feed": "instagram", "force": False, "timeout": 5}
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
async def test_no_cookies(tmp_path: Path, monkeypatch):
    """A logged-out browser produces a state that loads fine and crawls
    nothing, which on a walled platform reads as an empty site."""
    out = tmp_path / "s.json"
    with patch("crawlme.cli.session._browser_state", AsyncMock(return_value={"cookies": [], "origins": []})):
        with pytest.raises(SessionError, match="never logged in"):
            await capture("instagram", out)
    assert not out.exists(), "nothing is written when the login did not take"


@pytest.mark.asyncio
async def test_session_saved(tmp_path: Path):
    out = tmp_path / "nested" / "s.json"
    state = {"cookies": [{"name": "sessionid", "value": "x", "domain": ".instagram.com"}], "origins": []}
    with patch("crawlme.cli.session._browser_state", AsyncMock(return_value=state)):
        await capture("instagram", out)
    assert json.loads(out.read_text())["cookies"][0]["name"] == "sessionid"


@pytest.mark.asyncio
async def test_no_overwrite(tmp_path: Path, capsys):
    out = tmp_path / "s.json"
    out.write_text("{}")
    with pytest.raises(SystemExit):
        await cmd_session(_args(path=str(out)))
    assert "already exists" in capsys.readouterr().err
    assert out.read_text() == "{}", "the old session survived"


class _FakePage:
    def __init__(self, shut=False):
        self._shut = shut

    def is_closed(self):
        return self._shut

    async def goto(self, _url):
        return None

    async def close(self):
        self._shut = True


class _FakeCtx:
    """A context that hands back one session and records closing."""

    def __init__(self, state, page):
        self._state = state
        self._page = page
        self.closed = False

    async def new_page(self):
        return self._page

    async def storage_state(self):
        return self._state

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, ctx, connected=True):
        self._ctx = ctx
        self._connected = connected
        self.closed = False

    def is_connected(self):
        return self._connected

    async def new_context(self):
        return self._ctx

    async def close(self):
        self.closed = True
        self._connected = False


@contextlib.contextmanager
def _playwright(browser):
    """Stand in for the playwright module, installed or not.

    The capture imports it inside the call, so a patch needs the real
    module to exist and the bare CI lane has none. Supplying the module
    itself keeps these tests about the waiting, which is what breaks.
    """
    pw = AsyncMock()
    pw.chromium.launch = AsyncMock(return_value=browser)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=pw)
    cm.__aexit__ = AsyncMock(return_value=False)
    fake = types.ModuleType("playwright.async_api")
    fake.async_playwright = lambda: cm  # type: ignore[attr-defined]
    parent = sys.modules.get("playwright") or types.ModuleType("playwright")
    with patch.dict(sys.modules, {"playwright": parent, "playwright.async_api": fake}):
        yield pw


STATE = {"cookies": [{"name": "sessionid", "value": "x", "domain": ".instagram.com"}], "origins": []}


async def _capture(tmp_path, browser, **kw):
    with _playwright(browser):
        return await asyncio.wait_for(
            capture("instagram", tmp_path / "s.json", timeout_sec=kw.pop("timeout_sec", 5)), timeout=5
        )


@pytest.mark.asyncio
async def test_closing_the_tab_ends_it(tmp_path: Path):
    """A closed tab is not a closed context and not a disconnected
    browser, so listening for those two sat waiting through this one."""
    page = _FakePage(shut=True)
    browser = _FakeBrowser(_FakeCtx(STATE, page))
    state = await _capture(tmp_path, browser)
    assert state["cookies"]
    assert json.loads((tmp_path / "s.json").read_text())["cookies"]


@pytest.mark.asyncio
async def test_quitting_the_browser_says_so(tmp_path: Path):
    """A session lives in the browser and goes with it. Nothing can be
    read afterwards, so the way out is to say which way in works."""

    class _Gone(_FakeCtx):
        async def storage_state(self):
            raise RuntimeError("context has been closed")

    browser = _FakeBrowser(_Gone(STATE, _FakePage()), connected=False)
    with _playwright(browser):
        with pytest.raises(SessionError, match="Close the tab"):
            await capture("instagram", tmp_path / "s.json", timeout_sec=5)


@pytest.mark.asyncio
async def test_interrupt_keeps_the_login(tmp_path: Path):
    """The login already happened by the time anyone reaches for Ctrl-C,
    and reading it out still works."""
    ctx = _FakeCtx(STATE, _FakePage())
    browser = _FakeBrowser(ctx)

    async def interrupted(*_a, **_k):
        raise KeyboardInterrupt

    with _playwright(browser):
        with patch("crawlme.cli.session._wait_for_a_person", interrupted):
            out = tmp_path / "s.json"
            state = await capture("instagram", out, timeout_sec=5)
    assert state["cookies"]
    assert json.loads(out.read_text())["cookies"]


@pytest.mark.asyncio
async def test_nobody_logs_in_saves_nothing(tmp_path: Path):
    """A timeout is the one case where there is nothing worth keeping."""
    browser = _FakeBrowser(_FakeCtx(STATE, _FakePage()))
    with _playwright(browser):
        with pytest.raises(SessionError, match="within"):
            await capture("instagram", tmp_path / "s.json", timeout_sec=0.05)


def test_eof_is_not_a_keypress(monkeypatch):
    """stdin at end of input reads as ready and returns "". Counting
    that as Enter ended the wait instantly wherever stdin was not a
    terminal, and reported a login that never happened."""
    import io

    from crawlme.cli.session import _typed_enter

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("select.select", lambda *_a: ([sys.stdin], [], []))
    assert _typed_enter() is False


def test_a_typed_line_is_a_keypress(monkeypatch):
    import io

    from crawlme.cli.session import _typed_enter

    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    monkeypatch.setattr("select.select", lambda *_a: ([sys.stdin], [], []))
    assert _typed_enter() is True


def test_nothing_waiting_is_not_a_keypress(monkeypatch):
    from crawlme.cli.session import _typed_enter

    monkeypatch.setattr("select.select", lambda *_a: ([], [], []))
    assert _typed_enter() is False


@pytest.mark.parametrize("platform", ["instagram", "reddit"])
def test_session_platform_selection(platform):
    from crawlme.cli.session import _login_url, add_arguments

    parser = argparse.ArgumentParser()
    add_arguments(parser.add_subparsers())
    args = parser.parse_args(["session", "session.json", "--feed", platform])
    assert args.feed == platform
    assert _login_url(args.feed) == f"https://www.{platform}.com/"


def test_unknown_session_platform():
    from crawlme.cli.session import _login_url

    with pytest.raises(SessionError, match="Unknown session platform"):
        _login_url("unknown")


async def test_cdp_exports_only_selected_platform_and_leaves_browser_open(tmp_path):
    page = MagicMock(url="https://www.reddit.com/r/test/")
    context = MagicMock(pages=[page])
    context.storage_state = AsyncMock(
        return_value={
            "cookies": [
                {"name": "session", "value": "test", "domain": ".reddit.com"},
                {"name": "other", "value": "private", "domain": ".notreddit.com"},
            ],
            "origins": [
                {"origin": "https://www.reddit.com", "localStorage": [{"name": "key", "value": "value"}]},
                {"origin": "https://reddit.com.example.org", "localStorage": []},
            ],
        }
    )
    context.close = AsyncMock()
    browser = MagicMock(contexts=[context])
    browser.close = AsyncMock()
    out = tmp_path / "session.json"
    with _playwright(browser) as pw, patch("crawlme.cli.session._wait_for_a_person", AsyncMock(return_value=True)):
        pw.chromium.connect_over_cdp = AsyncMock(return_value=browser)
        state = await capture("reddit", out, cdp_url="http://127.0.0.1:9222")
        pw.chromium.connect_over_cdp.assert_awaited_once_with("http://127.0.0.1:9222", timeout=30_000)
        pw.chromium.launch.assert_not_awaited()
    assert [c["domain"] for c in state["cookies"]] == [".reddit.com"]
    assert [o["origin"] for o in state["origins"]] == ["https://www.reddit.com"]
    assert json.loads(out.read_text()) == state
    context.new_page.assert_not_called()
    context.close.assert_not_awaited()
    browser.close.assert_not_awaited()


async def test_cdp_connection_failure_saves_nothing(tmp_path):
    out = tmp_path / "session.json"
    with _playwright(MagicMock()) as pw:
        pw.chromium.connect_over_cdp = AsyncMock(side_effect=OSError("unreachable"))
        with pytest.raises(SessionError, match="could not connect"):
            await capture("reddit", out, cdp_url="http://127.0.0.1:9222")
    assert not out.exists()


async def test_cdp_timeout_keeps_browser_open_and_existing_file(tmp_path):
    context = MagicMock(pages=[MagicMock(url="https://www.reddit.com/")])
    context.close = AsyncMock()
    browser = MagicMock(contexts=[context])
    browser.close = AsyncMock()
    out = tmp_path / "session.json"
    out.write_text("existing")
    with _playwright(browser) as pw, patch("crawlme.cli.session._wait_for_a_person", AsyncMock(return_value=False)):
        pw.chromium.connect_over_cdp = AsyncMock(return_value=browser)
        with pytest.raises(SessionError, match="within"):
            await capture("reddit", out, cdp_url="http://127.0.0.1:9222")
    assert out.read_text() == "existing"
    context.close.assert_not_awaited()
    browser.close.assert_not_awaited()
