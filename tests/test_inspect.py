"""Tests for the inspect command: read-only rendering of a run's data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from crawlme.cli.inspect import InspectError, cmd_inspect, inspect_task
from crawlme.config import Settings
from crawlme.schemas import URL, CrawlGoal, CrawlTask, Page
from crawlme.storage.sqlite.crawl_db import SqliteCrawlDb


def _goal(prompt: str) -> CrawlGoal:
    return CrawlGoal(prompt=prompt)


def _page(key: str, title: str = "Title") -> Page:
    return Page(
        page_id=f"p-{key}",
        url_key=key,
        url=URL(
            raw=f"https://example.com/{key}",
            canonical=f"https://example.com/{key}",
            url_key=key,
            reg_domain="example.com",
        ),
        title=f"{title} {key}",
        plain_text="hello world",
    )


def _analysis(url_key: str, goal_id: str, classification: str, relevance: float) -> dict:
    return {
        "analysis_id": f"a-{goal_id[:4]}-{url_key}",
        "page_id": f"p-{url_key}",
        "url_key": url_key,
        "goal_id": goal_id,
        "classification": classification,
        "relevance_score": relevance,
        "summary": f"summary of {url_key}",
        "feedback": {"hub_score": 0.4},
        "model": "stub-model",
        "prompt_version": "v2.4",
        "tokens_used": 10,
        "analyzed_at": "2026-01-01T00:00:00Z",
    }


async def _write_run(root: Path, ts: str, *, task_id: str = "task1") -> Path:
    """A run with two goals and analyses under each."""
    original = _goal("find rust posts")
    replay_goal = _goal("filter the most valuable articles")
    run_dir = root / ts
    (run_dir / "db").mkdir(parents=True)
    db = SqliteCrawlDb(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
    await db.start()
    db.save_goal(original.model_dump(mode="json"))
    db.save_goal(replay_goal.model_dump(mode="json"))
    db.save_task(CrawlTask(task_id=task_id, goal_id=original.goal_id).model_dump(mode="json"))
    for key in ("a", "b", "c"):
        db.save_page(_page(key))
    db.save_analysis(_analysis("a", original.goal_id, "RELEVANT", 0.9))
    db.save_analysis(_analysis("b", original.goal_id, "RELEVANT", 0.5))
    db.save_analysis(_analysis("c", original.goal_id, "IRRELEVANT", 0.1))
    db.save_analysis(_analysis("a", replay_goal.goal_id, "IRRELEVANT", 0.0))
    await db.close()
    return run_dir


def _cfg(tmp_path: Path) -> Settings:
    return Settings(llm_api_key="", llm_base_url="", llm_model="", result_dir=tmp_path)


@pytest.mark.asyncio
async def test_inspect_selects_the_original_goal_by_default(tmp_path):
    await _write_run(tmp_path, "20260101_000001")
    data = await inspect_task(_cfg(tmp_path), "task1")

    assert data.pages and len(data.pages) == 3
    assert len(data.analyses) == 3  # the original goal's analyses only
    assert data.goal_counts[data.goal_id] == 3
    # The replay goal is listed with its count.
    others = [g for g in data.goals if g["goal_id"] != data.goal_id]
    assert len(others) == 1
    assert data.goal_counts[others[0]["goal_id"]] == 1


@pytest.mark.asyncio
async def test_inspect_goal_flag_selects_a_replay_goal(tmp_path):
    await _write_run(tmp_path, "20260101_000001")
    others = [g for g in (await inspect_task(_cfg(tmp_path), "task1")).goals]
    replay_goal = next(g["goal_id"] for g in others if g["prompt"] != "find rust posts")

    data = await inspect_task(_cfg(tmp_path), "task1", goal_id=replay_goal)

    assert data.goal_id == replay_goal
    assert len(data.analyses) == 1
    assert data.analyses[0]["classification"] == "IRRELEVANT"


@pytest.mark.asyncio
async def test_inspect_unknown_goal_raises(tmp_path):
    await _write_run(tmp_path, "20260101_000001")
    with pytest.raises(InspectError, match="not found"):
        await inspect_task(_cfg(tmp_path), "task1", goal_id="nope")


@pytest.mark.asyncio
async def test_inspect_missing_task_raises(tmp_path):
    with pytest.raises(Exception, match="task1"):
        await inspect_task(_cfg(tmp_path), "task1")


@pytest.mark.asyncio
async def test_cmd_inspect_prints_summary(tmp_path, monkeypatch, capsys):
    await _write_run(tmp_path, "20260101_000001")
    monkeypatch.setattr("crawlme.cli.inspect.Settings", lambda: _cfg(tmp_path))

    await cmd_inspect(argparse.Namespace(task_id="task1", goal=None, export=None))

    out = capsys.readouterr().out
    assert "task:      task1" in out
    assert "pages:     3 fetched" in out
    assert "2 RELEVANT" in out
    assert "top relevant:" in out
    assert "Title a" in out  # highest relevance first


@pytest.mark.asyncio
async def test_cmd_inspect_labels_goal_roles(tmp_path, monkeypatch, capsys):
    await _write_run(tmp_path, "20260101_000001")
    monkeypatch.setattr("crawlme.cli.inspect.Settings", lambda: _cfg(tmp_path))

    await cmd_inspect(argparse.Namespace(task_id="task1", goal=None, export=None))

    out = capsys.readouterr().out
    assert "(original)" in out  # the selected goal line
    assert "(1, replay)" in out  # the other-goals line


@pytest.mark.asyncio
async def test_cmd_inspect_exports_json(tmp_path, monkeypatch, capsys):
    await _write_run(tmp_path, "20260101_000001")
    monkeypatch.setattr("crawlme.cli.inspect.Settings", lambda: _cfg(tmp_path))

    await cmd_inspect(argparse.Namespace(task_id="task1", goal=None, export="json"))

    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 3
    assert rows[0]["url"] == "https://example.com/a"
    assert rows[0]["classification"] == "RELEVANT"
    assert rows[0]["hub_score"] == 0.4
    assert rows[0]["title"]


@pytest.mark.asyncio
async def test_cmd_inspect_exports_csv(tmp_path, monkeypatch, capsys):
    await _write_run(tmp_path, "20260101_000001")
    monkeypatch.setattr("crawlme.cli.inspect.Settings", lambda: _cfg(tmp_path))

    await cmd_inspect(argparse.Namespace(task_id="task1", goal=None, export="csv"))

    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert lines[0].startswith("url,url_key,title,published_at,goal_id,classification")
    assert len(lines) == 4  # header + 3 analyses
    assert "https://example.com/a" in lines[1]


@pytest.mark.asyncio
async def test_json_export_carries_evidence(tmp_path, monkeypatch, capsys):
    """The json form is what something other than a person reads.

    A value without the page text behind it is a claim; with it, whoever
    renders this can let the reader check it.
    """
    run_dir = await _write_run(tmp_path, "20260101_000001")
    db = SqliteCrawlDb(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
    await db.start()
    db.save_analysis(
        {
            "analysis_id": "a-extracted",
            "page_id": "p-k1",
            "url_key": "k1",
            "goal_id": _goal("find rust posts").goal_id,
            "classification": "RELEVANT",
            "relevance_score": 0.9,
            "extracted": {"merchant_name": {"value": "Molly Tea", "evidence": "Molly Tea is treating you"}},
            "spec_version": "abc123",
            "model": "stub-model",
            "analyzed_at": "2026-01-02T00:00:00Z",
        }
    )
    await db._write_queue.join()
    await db.close()

    monkeypatch.setattr("crawlme.cli.inspect.Settings", lambda: _cfg(tmp_path))
    await cmd_inspect(argparse.Namespace(task_id="task1", goal=None, export="json"))
    rows = json.loads(capsys.readouterr().out)

    row = next(r for r in rows if r["analyzed_at"] == "2026-01-02T00:00:00Z")
    assert row["extracted"]["merchant_name"]["value"] == "Molly Tea"
    assert row["extracted"]["merchant_name"]["evidence"] == "Molly Tea is treating you"
    assert row["spec_version"] == "abc123"
    assert "url_key" in row and "published_at" in row


@pytest.mark.asyncio
async def test_csv_export_leaves_the_extracted_fields_out(tmp_path, monkeypatch, capsys):
    """Every goal declares its own fields, so there is no stable column
    set; inventing one per export would make two exports disagree."""
    await _write_run(tmp_path, "20260101_000001")
    monkeypatch.setattr("crawlme.cli.inspect.Settings", lambda: _cfg(tmp_path))
    await cmd_inspect(argparse.Namespace(task_id="task1", goal=None, export="csv"))
    header = capsys.readouterr().out.splitlines()[0]
    assert "extracted" not in header
