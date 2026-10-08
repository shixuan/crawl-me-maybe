"""Read existing runs without schema setup, writers or log handlers."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
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
    items: list[dict[str, Any]] = field(default_factory=list)


def has_items(con: sqlite3.Connection) -> bool:
    return (
        con.execute("SELECT 1 FROM sqlite_master WHERE name='analysis_items' AND type='table'").fetchone() is not None
    )


def read_items(con: sqlite3.Connection) -> list[dict[str, Any]]:
    if not has_items(con):
        return [
            {**dict(row), "item_id": dict(row).get("analysis_id", row["url_key"]), "evidence_json": "[]"}
            for row in con.execute(queries.LATEST_ANALYSES)
        ]
    return [dict(row) for row in con.execute(queries.items_query(modern=has_items(con)))]


def read_results(db: Path) -> RunResults:
    with closing(connect(db)) as con:
        # Keep the three tables consistent if a crawl commits while they are read.
        con.execute("BEGIN")
        return RunResults(
            goals=[dict(row) for row in con.execute(queries.GOALS)],
            pages=[dict(row) for row in con.execute(queries.PAGES)],
            analyses=[dict(row) for row in con.execute(queries.LATEST_ANALYSES + " ORDER BY a.analyzed_at, a.rowid")],
            items=read_items(con),
        )
