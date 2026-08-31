from __future__ import annotations

import argparse
import json
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
