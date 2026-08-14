"""Setup function: wires the root logger from Settings."""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING

from crawlme.logging.formatters import ConsoleFormatter, JsonFormatter

if TYPE_CHECKING:
    from crawlme.config import Settings


_OFF = logging.CRITICAL + 10


def setup_logging(settings: Settings, *, force: bool = False) -> None:
    """Configure the root logger from *settings*.

    Idempotent: only configures once unless *force* is True.

    Calling convention (two deliberate call sites):
      - CLI: ``_cmd_run`` calls once with force=True AFTER applying
        flag overrides, the single place where per-run log settings
        land.  Never call before flags are known, or the flag values
        will silently not apply (idempotency swallows the second call).
      - engine.run(): calls again WITHOUT force as a safety net for
        library users who never went through the CLI; in the CLI flow
        this call is a no-op.

    log_level values: DEBUG, INFO, WARNING, ERROR, CRITICAL, OFF.
    OFF disables all output: no handler is added.
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
    for noisy in ("httpx", "httpcore", "trafilatura", "urllib3", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def to_file(path: str) -> None:
    """Also write logs to *path* (e.g. <run_dir>/log).

    Idempotent per path: callers attach early and late, and only the
    first call wins.
    """
    root = logging.getLogger()
    target = os.path.abspath(path)
    for existing in root.handlers:
        if isinstance(existing, logging.FileHandler) and os.path.abspath(existing.baseFilename) == target:
            return
    h = logging.FileHandler(path)
    h.setLevel(root.level)
    h.setFormatter(ConsoleFormatter())
    root.addHandler(h)


def _level(name: str) -> int:
    if name.upper() in ("OFF", "NONE", ""):
        return _OFF
    return getattr(logging, name.upper(), logging.INFO)
