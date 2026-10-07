"""Read existing runs without schema setup, writers or log handlers."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crawlme.storage import queries


def readonly_uri(db: Path) -> str:
    return db.resolve().as_uri() + "?mode=ro"


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(readonly_uri(db), uri=True)
    con.row_factory = sqlite3.Row
    return con


@dataclass
class RunResults:
    goals: list[dict[str, Any]]
    pages: list[dict[str, Any]]
    analyses: list[dict[str, Any]]


def read_results(db: Path) -> RunResults:
    with closing(connect(db)) as con:
        # Keep the three tables consistent if a crawl commits while they are read.
        con.execute("BEGIN")
        return RunResults(
            goals=[dict(row) for row in con.execute(queries.GOALS)],
            pages=[dict(row) for row in con.execute(queries.PAGES)],
            analyses=[dict(row) for row in con.execute(queries.LATEST_ANALYSES + " ORDER BY a.analyzed_at, a.rowid")],
        )
