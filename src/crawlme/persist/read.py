"""Read existing runs without schema setup, writers or log handlers."""

from __future__ import annotations

import datetime
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

from crawlme.digest import Page
from crawlme.errors import Error, Kind
from crawlme.persist import queries
from crawlme.runtime import CrawlGoal, CrawlTask
from crawlme.urls import URL


def readonly_uri(db: Path) -> str:
    return db.resolve().as_uri() + "?mode=ro"


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(readonly_uri(db), uri=True)
    con.row_factory = sqlite3.Row
    return con


@dataclass
class Results:
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


def read_results(db: Path) -> Results:
    with closing(connect(db)) as con:
        # Keep the three tables consistent if a crawl commits while they are read.
        con.execute("BEGIN")
        return Results(
            goals=[dict(row) for row in con.execute(queries.GOALS)],
            pages=[dict(row) for row in con.execute(queries.PAGES)],
            analyses=[dict(row) for row in con.execute(queries.LATEST_ANALYSES + " ORDER BY a.analyzed_at, a.rowid")],
            items=read_items(con),
        )


async def find_run_dir(result_dir: Path, task_id: str) -> tuple[Path, dict[str, Any]]:
    """Find the run database containing task_id; raise Error if absent."""
    seen: dict[str, str] = {}
    for db_path in sorted(result_dir.glob("*/db/crawl.db"), reverse=True):
        try:
            async with aiosqlite.connect(readonly_uri(db_path), uri=True) as conn:
                conn.row_factory = aiosqlite.Row
                cur = await conn.execute("SELECT * FROM crawl_tasks WHERE task_id = ?", (task_id,))
                row = await cur.fetchone()
                if row is not None:
                    return db_path.parent.parent, dict(row)
                cur = await conn.execute("SELECT task_id FROM crawl_tasks")
                for r in await cur.fetchall():
                    seen[r["task_id"]] = db_path.parent.parent.name
        except sqlite3.Error:
            # A stray or unreadable database; keep scanning.
            continue
    if seen:
        detail = ", ".join(f"{tid} ({ts})" for tid, ts in sorted(seen.items()))
        raise Error(
            f"task {task_id} not found; runs under {result_dir} hold: {detail}",
            source="persist",
            code="task_missing",
            kind=Kind.NOT_FOUND,
            details={"task_id": task_id},
        )
    raise Error(
        f"task {task_id} not found: no run databases under {result_dir}",
        source="persist",
        code="task_missing",
        kind=Kind.NOT_FOUND,
        details={"task_id": task_id},
    )


def goal_from_row(row: dict[str, Any]) -> CrawlGoal:
    """Restore a goal and its extraction specification from a database row."""
    data = dict(row)
    data["keywords"] = json.loads(data.get("keywords") or "[]")
    data["extraction_spec"] = json.loads(data["extraction_spec"]) if data.get("extraction_spec") else None
    return CrawlGoal.model_validate(data)


def task_from_row(row: dict[str, Any]) -> CrawlTask:
    """Restore a task, including its JSON-encoded counters."""
    return CrawlTask.model_validate({**row, "counters": json.loads(row.get("counters") or "{}")})


def page_from_row(row: dict[str, Any]) -> Page:
    """Rebuild a Page from a stored row (the frozen corpus)."""
    return Page(
        page_id=row["page_id"],
        url_key=row["url_key"],
        url=URL.model_validate(json.loads(row["url_json"])),
        raw_html_path=row.get("raw_html_path", ""),
        title=row.get("title"),
        markdown=row.get("markdown"),
        plain_text=row.get("plain_text"),
        metadata=json.loads(row.get("metadata_json") or "{}"),
        text_hash=row.get("text_hash", ""),
        text_len=row.get("text_len", 0),
        published_at=_parse_optional_ts(row.get("published_at")),
        extracted_at=_parse_ts(row.get("extracted_at")),
        extraction_status=row.get("extraction_status", "OK"),
    )


def _parse_ts(value: Any) -> datetime.datetime:
    if isinstance(value, str) and value:
        return datetime.datetime.fromisoformat(value)
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_optional_ts(value: Any) -> datetime.datetime | None:
    """Parse a nullable timestamp, preserving missing values as None."""
    if isinstance(value, str) and value:
        try:
            return datetime.datetime.fromisoformat(value)
        except ValueError:
            return None
    return None
