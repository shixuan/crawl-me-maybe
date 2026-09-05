"""Setup function: wires the root logger from Settings.

What belongs at each level, so the answer is not decided once per
investigation:

  ERROR    the run cannot go on, or a whole stage stopped working
  WARNING  the run is producing something wrong or incomplete and is
           carrying on anyway. A reader who ignores it gets a report
           that looks fine and is not
  INFO     what is happening, for the person who started the run. One
           line each time something is handled, naming it by its
           address. No url_keys, no byte counts, no ratios
  DEBUG    the same events counted rather than described, plus the
           mechanics that have no readable form

Two tests. WARNING against INFO: would ignoring this line leave someone
believing a result that is not true? Payloads dropped by their content
type sat at DEBUG and five accounts were read weeks out of date without
a word.

INFO against DEBUG: would the person who typed the command understand
this line and care? "read 63 posts from timhortons" passes. "kept=6
bytes=2482493" does not, however much it helped whoever was debugging
the day it was written.

Readable is not the same as sparse. Both levels run per item, and the
split is vocabulary rather than density. A page fetched, a candidate
scored, a page judged, each gets an INFO line in words and a DEBUG
line in numbers. Moving the per-item lines to DEBUG alone left four
workers running in parallel behind a terminal that printed once a
minute, which reads as a stall, and a periodic "25 pages read" stood
in for the work without showing any of it.

INFO speaks when the wait begins, not when it ends. A line that only
arrives with the answer says nothing for as long as the answer takes,
and one seed proposal took over two minutes, which read as a hang and
was interrupted twice. Anything that can take seconds announces itself
first.

A field name says what it measured, not what it is about. `took` reads
as the time a call spent and was the wall clock around an await, which
on a busy loop is a different number.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING

from crawlme.logging.formatters import ConsoleFormatter, JsonFormatter

if TYPE_CHECKING:
    from crawlme.config import Settings


_OFF = logging.CRITICAL + 10
# Startup lines held for a file that does not exist yet. Enough for the
# whole startup phase and small enough to forget about if no file ever
# arrives.
_BACKLOG_LIMIT = 1000


class _Backlog(logging.Handler):
    """Keeps records until there is a file to put them in.

    The run directory is named by the scheduler, so nothing can be
    written to disk until it exists. Everything logged before that used
    to reach the terminal alone, which is exactly the part a person
    goes looking for afterwards, and afterwards the terminal is gone.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.records) < _BACKLOG_LIMIT:
            self.records.append(record)


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
    root.addHandler(_Backlog())

    # Quiet noisy third-party loggers. litellm attaches a handler of
    # its own and never sets a level, so at INFO it announced every
    # completion twice, once through its handler and once through ours.
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
