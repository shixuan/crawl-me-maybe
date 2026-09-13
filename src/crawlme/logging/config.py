"""Configure console/file logging and buffer startup records.

ERROR reports failed stages; WARNING reports degraded results; INFO describes
progress using readable addresses; DEBUG carries diagnostic counters and IDs."""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING

from crawlme.logging.formatters import ConsoleFormatter, JsonFormatter

if TYPE_CHECKING:
    from crawlme.config import Settings


_OFF = logging.CRITICAL + 10
# Bound startup records retained before the log file is attached.
_BACKLOG_LIMIT = 1000


class _Backlog(logging.Handler):
    """Buffer startup records until the run log file exists."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.records) < _BACKLOG_LIMIT:
            self.records.append(record)


def setup_logging(settings: Settings, *, force: bool = False) -> None:
    """Configure handlers and levels; existing handlers are preserved unless force=True.

    Apply CLI overrides before calling. OFF installs no handlers."""
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
    root.addHandler(_Backlog())

    # Suppress routine third-party logs and duplicate completion messages.
    for noisy in (
        "httpx",
        "httpcore",
        "trafilatura",
        "urllib3",
        "aiosqlite",
        "LiteLLM",
        "LiteLLM Router",
        "LiteLLM Proxy",
    ):
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
    # What was said before the file existed, in the order it was said.
    # The backlog goes with it: from here the file is the record.
    for backlog in [x for x in root.handlers if isinstance(x, _Backlog)]:
        for record in backlog.records:
            h.handle(record)
        root.removeHandler(backlog)


def _level(name: str) -> int:
    if name.upper() in ("OFF", "NONE", ""):
        return _OFF
    return getattr(logging, name.upper(), logging.INFO)
