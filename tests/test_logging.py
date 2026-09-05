"""Tests for crawlme.logging (configuration and formatters)."""

from __future__ import annotations

import logging

from crawlme.config import Settings
from crawlme.logging import setup_logging
from crawlme.logging.config import _OFF, _level


def test_level_off():
    assert _level("OFF") >= logging.CRITICAL + 1
    assert _level("off") >= logging.CRITICAL + 1
    assert _level("none") >= logging.CRITICAL + 1
    assert _level("") >= logging.CRITICAL + 1


def test_level_standard():
    assert _level("DEBUG") == logging.DEBUG
    assert _level("INFO") == logging.INFO
    assert _level("WARNING") == logging.WARNING
    assert _level("ERROR") == logging.ERROR


def test_level_unknown():
    assert _level("garbage") == logging.INFO


def test_off_no_handler():
    """With log_level=OFF, no handler should be added."""
    root = logging.getLogger()
    root.handlers.clear()

    setup_logging(Settings(log_level="OFF"), force=True)
    assert len(root.handlers) == 0
    assert root.level >= _OFF


def test_console_handler():
    """Normal log level should add a console handler.

    Counted by kind, not by total: the backlog that holds startup lines
    for the run file sits beside it until that file is attached."""
    root = logging.getLogger()
    setup_logging(Settings(log_level="DEBUG", log_format="console"), force=True)
    console = [h for h in root.handlers if type(h) is logging.StreamHandler]
    assert len(console) == 1
    assert root.level == logging.DEBUG


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


def test_backlog_is_dropped_once_replayed(tmp_path):
    """Kept past the attach it would hold every record of the run, and
    replay them again the next time a file is attached."""
    from crawlme.logging import to_file
    from crawlme.logging.config import _Backlog

    setup_logging(Settings(log_level="INFO"), force=True)
    logging.getLogger("startup").info("once")
    to_file(str(tmp_path / "log"))

    assert not [h for h in logging.getLogger().handlers if isinstance(h, _Backlog)]
    second = tmp_path / "second"
    to_file(str(second))
    assert "once" not in second.read_text()
