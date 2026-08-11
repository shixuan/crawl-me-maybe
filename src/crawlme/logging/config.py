"""Setup function — wires the root logger from Settings."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from crawlme.logging.formatters import ConsoleFormatter, JsonFormatter

if TYPE_CHECKING:
    from crawlme.config import Settings


_OFF = logging.CRITICAL + 10


def setup_logging(settings: Settings, *, force: bool = False) -> None:
    """Configure the root logger from *settings*.

    Idempotent — only configures once unless *force* is True.

    log_level values: DEBUG, INFO, WARNING, ERROR, CRITICAL, OFF.
    OFF disables all output — no handler is added.
    """
    root = logging.getLogger()
    if root.handlers and not force:
        return

    level = _level(settings.log_level)
    root.setLevel(level)
    root.handlers.clear()

    if level >= _OFF:
        return

    h = logging.StreamHandler(sys.stderr)
    h.setLevel(level)

    if settings.log_format == "json":
        h.setFormatter(JsonFormatter())
    else:
        h.setFormatter(ConsoleFormatter())

    root.addHandler(h)

    # Quiet noisy third-party loggers.
    for noisy in ("httpx", "httpcore", "trafilatura", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _level(name: str) -> int:
    if name.upper() in ("OFF", "NONE", ""):
        return _OFF
    return getattr(logging, name.upper(), logging.INFO)
