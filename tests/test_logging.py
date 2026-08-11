"""Tests for crawlme.logging — configuration and formatters."""

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


def test_level_unknown_defaults_to_info():
    assert _level("garbage") == logging.INFO


def test_off_adds_no_handler():
    """With log_level=OFF, no handler should be added."""
    root = logging.getLogger()
    root.handlers.clear()

    setup_logging(Settings(log_level="OFF"), force=True)
    assert len(root.handlers) == 0
    assert root.level >= _OFF


def test_console_adds_handler():
    """Normal log level should add a handler."""
    root = logging.getLogger()
    setup_logging(Settings(log_level="DEBUG", log_format="console"), force=True)
    assert len(root.handlers) == 1
    assert root.level == logging.DEBUG


def test_setup_is_idempotent():
    """Second call without force should be a no-op."""
    root = logging.getLogger()
    setup_logging(Settings(log_level="INFO"), force=True)
    n = len(root.handlers)
    setup_logging(Settings(log_level="DEBUG", log_format="console"))
    assert len(root.handlers) == n  # unchanged
