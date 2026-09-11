"""What the dashboard hands the page about when a result runs."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

import serve

WITH_DATES = "starts_on TEXT DEFAULT '', ends_on TEXT DEFAULT '',"


def _run_db(root: Path, *, dates: bool) -> Path:
    db = root / "20260101_000000" / "db" / "crawl.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE crawl_goals (goal_id TEXT, prompt TEXT, extraction_spec TEXT, created_at TEXT);"
        "CREATE TABLE crawl_tasks (task_id TEXT, goal_id TEXT, start_at TEXT);"
        "CREATE TABLE pages (url_key TEXT, url_json TEXT, title TEXT, plain_text TEXT, "
        "markdown TEXT, published_at TEXT);"
        "CREATE TABLE analyses (url_key TEXT, goal_id TEXT, classification TEXT, "
        f"relevance_score REAL, {WITH_DATES if dates else ''} summary TEXT, "
        "tags_json TEXT, extracted_json TEXT, model TEXT, analyzed_at TEXT);"
    )
    con.execute("INSERT INTO crawl_goals VALUES ('g1', 'find things', '{}', '2026-01-01')")
    con.execute("INSERT INTO crawl_tasks VALUES ('t1', 'g1', '2026-01-01')")
    con.execute("INSERT INTO pages VALUES ('k1', '{\"canonical\": \"https://x/1\"}', 'One', '', '', '')")
    if dates:
        con.execute(
            "INSERT INTO analyses VALUES ('k1', 'g1', 'RELEVANT', 0.9, '', '2099-01-01', "
            "'s', '[]', '{}', 'm', '2026-01-01')"
        )
    else:
        con.execute("INSERT INTO analyses VALUES ('k1', 'g1', 'RELEVANT', 0.9, 's', '[]', '{}', 'm', '2026-01-01')")
    con.commit()
    con.close()
    return db


@pytest.mark.parametrize("dates", [True, False])
def test_a_run_reads_whether_or_not_it_stored_dates(tmp_path: Path, dates: bool) -> None:
    """A run from before the dates were stored must still open."""
    _run_db(tmp_path, dates=dates)
    rows = serve._results(tmp_path, "20260101_000000")["rows"]
    assert len(rows) == 1
    assert rows[0]["when"] == ("open" if dates else "undated")


def test_an_end_alone_is_carried_as_running(tmp_path: Path) -> None:
    _run_db(tmp_path, dates=True)
    row = serve._results(tmp_path, "20260101_000000")["rows"][0]
    assert row["starts_on"] == ""
    assert row["ends_on"] == "2099-01-01"
    assert row["when"] == "open"
