"""Log formatters: human-readable console and machine-readable JSON."""

from __future__ import annotations

import logging


class ConsoleFormatter(logging.Formatter):
    """`timestamp level [name] message`: compact, grep-friendly."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )


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
