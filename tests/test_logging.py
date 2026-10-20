"""Tests for crawlme.logging (configuration and formatters)."""

from __future__ import annotations

import io
import json
import logging
from types import SimpleNamespace

import pytest

from crawlme.config import Settings
from crawlme.logging import setup_logging
from crawlme.logging.config import _OFF


def test_off_no_handler():
    """With log_level=OFF, no handler should be added."""
    root = logging.getLogger()
    root.handlers.clear()

    setup_logging(Settings(log_level="OFF"), force=True)
    assert len(root.handlers) == 0
    assert root.level >= _OFF


def test_setup_idempotent():
    """Second call without force should be a no-op."""
    root = logging.getLogger()
    setup_logging(Settings(log_level="INFO"), force=True)
    n = len(root.handlers)
    setup_logging(Settings(log_level="DEBUG", log_format="console"))
    assert len(root.handlers) == n  # unchanged


def test_startup_lines_reach_the_file(tmp_path):
    """The run directory is named by the scheduler, so everything logged
    before it exists used to reach the terminal alone. That is the part
    a person goes looking for afterwards, when the terminal is gone."""
    from crawlme.logging import to_file

    setup_logging(Settings(log_level="INFO"), force=True)
    logging.getLogger("startup").info("said before the file existed")
    path = tmp_path / "log"
    to_file(str(path))
    logging.getLogger("startup").info("said after")

    written = path.read_text()
    assert "said before the file existed" in written
    assert "said after" in written
    assert written.index("said before") < written.index("said after")


@pytest.fixture
def terminal(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr("crawlme.logging.config.sys", SimpleNamespace(stderr=stream))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield stream
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def test_color_stays_out_of_file(terminal, tmp_path):
    from crawlme.logging import to_file

    setup_logging(Settings(log_level="INFO", log_format="console"), force=True)
    logger = logging.getLogger("crawlme.analysis.analyzer")
    logger.info("before attachment")
    path = tmp_path / "log"
    to_file(str(path))
    try:
        raise ValueError("failed page")
    except ValueError:
        logger.exception("after attachment")
    console = terminal.getvalue()
    written = path.read_text()
    assert "\033[32mINFO" in console
    assert "\033[31mERROR" in console
    assert console.endswith("ValueError: failed page\033[0m\n")
    assert "\033[" not in written
    assert "before attachment" in written
    assert "after attachment" in written
    assert "ValueError: failed page" in written


@pytest.mark.parametrize("mode", ["redirected", "no_color", "dumb", "json"])
def test_plain_output(terminal, monkeypatch, mode):
    if mode == "redirected":
        monkeypatch.setattr(terminal, "isatty", lambda: False)
    elif mode == "no_color":
        monkeypatch.setenv("NO_COLOR", "1")
    elif mode == "dumb":
        monkeypatch.setenv("TERM", "dumb")
    setup_logging(Settings(log_level="INFO", log_format="json" if mode == "json" else "console"), force=True)
    logging.getLogger("crawlme.analysis.analyzer").info("judged %s", "example.com")
    output = terminal.getvalue()
    assert "\033[" not in output
    assert "judged example.com" in output
    if mode == "json":
        assert json.loads(output)["msg"] == "judged example.com"
