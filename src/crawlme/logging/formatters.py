"""Log formatters: human-readable console and machine-readable JSON."""

from __future__ import annotations

import logging

_RESET = "\033[0m"
_STAGES = (
    ("crawlme.digest", "\033[36m"),
    ("crawlme.discovery", "\033[36m"),
    ("crawlme.platforms", "\033[36m"),
    ("crawlme.scheduler.workers.fetch", "\033[36m"),
    ("crawlme.scheduler.workers.discovery", "\033[36m"),
    ("crawlme.pioneer.ranker", "\033[94m"),
    ("crawlme.scheduler.workers.ranking", "\033[94m"),
    ("crawlme.analysis", "\033[32m"),
    ("crawlme.scheduler.workers.analysis", "\033[32m"),
    ("crawlme.pioneer.goal_enhancer", "\033[35m"),
    ("crawlme.pioneer.seed_expander", "\033[35m"),
    ("crawlme.dedup", "\033[35m"),
    ("crawlme.llm", "\033[35m"),
)


class ConsoleFormatter(logging.Formatter):
    """`timestamp level [name] message`: compact, grep-friendly."""

    def __init__(self, *, color: bool = False) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self._color:
            return text
        if record.levelno >= logging.ERROR:
            color = "\033[31m"
        elif record.levelno >= logging.WARNING:
            color = "\033[33m"
        elif record.levelno <= logging.DEBUG:
            color = ""
        else:
            color = next(
                (color for name, color in _STAGES if record.name == name or record.name.startswith(name + ".")), ""
            )
        timestamp, body = text.split(" ", 1)
        body = f"{color}{body}{_RESET}" if color else body
        return f"{timestamp} {body}"


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        import json

        return json.dumps(
            {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
                "func": record.funcName,
                "line": record.lineno,
            },
            ensure_ascii=False,
            default=str,
        )
