from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from crawlme.cli.session import SessionError, _login_url, capture, cmd_session


def _args(**kw) -> argparse.Namespace:
    base = {"path": "s.json", "feed": "instagram", "force": False, "timeout": 5}
    base.update(kw)
    return argparse.Namespace(**base)


def test_login_url():
    assert _login_url("instagram") == "https://www.instagram.com/"


def test_open_platform():
    """The adapters answer this, so no platform list lives here."""
    with pytest.raises(SessionError, match="needs no session"):
        _login_url("rss")


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
        yield


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


def test_advice_names_real_flags(tmp_path: Path, capsys):
    """It named --feed long after crawl run stopped having one, so the
    line could not be copied out of it."""
    import argparse
    import pathlib
    import re

    out = tmp_path / "s.json"
    with patch("crawlme.cli.session.capture", AsyncMock(return_value=STATE)):
        asyncio.run(cmd_session(argparse.Namespace(path=str(out), feed="instagram", force=True, timeout=5)))
    advice = next(ln for ln in capsys.readouterr().out.splitlines() if "crawl run" in ln)
    named = {w for w in advice.split() if w.startswith("--")}
    cli = pathlib.Path("src/crawlme/cli/__init__.py").read_text()
    real = set(re.findall(r'run_p\.add_argument\(\s*"(--[\w-]+)"', cli))
    assert named <= real, f"advice names flags crawl run does not have: {named - real}"


def test_no_platform_is_named_in_the_code():
    """One platform needs a session today; the next one must not need
    this file edited."""
    import pathlib
    import re

    src = pathlib.Path("src/crawlme/cli/session.py").read_text()
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    assert "instagram" not in code.lower()
