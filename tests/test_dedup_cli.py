import argparse
import sqlite3
from unittest.mock import AsyncMock

import pytest

from crawlme.cli.dedup import cmd_dedup
from crawlme.config import Settings
from crawlme.dedup.grouper import Group
from crawlme.schemas import URL, AnalysisResult, CrawlGoal, CrawlTask, Page
from crawlme.storage.sqlite import SqliteStorage


@pytest.mark.parametrize("fail", [False, True])
async def test_existing_results_command(tmp_path, monkeypatch, capsys, fail):
    storage = SqliteStorage.create(tmp_path)
    await storage.start()
    goal = CrawlGoal(prompt="find gifts")
    other = CrawlGoal(prompt="another goal")
    task = CrawlTask(goal_id=other.goal_id, state="COMPLETED")
    storage.save_goal(goal.model_dump(mode="json"))
    storage.save_goal(other.model_dump(mode="json"))
    storage.save_task(task.model_dump(mode="json"))
    page = Page(url=URL(raw="https://example.com", canonical="https://example.com", url_key="a"), url_key="a")
    storage.save_page(page)
    analysis = AnalysisResult(
        page_id=page.page_id, url_key="a", goal_id=goal.goal_id, classification="RELEVANT", summary="gift"
    )
    storage.save_analysis(analysis.model_dump(mode="json"))
    await storage.save_groups(
        goal.goal_id, "old", [{"members": [analysis.analysis_id], "overview": "old"}], model="test", version="v1"
    )
    await storage.close()
    calls = []

    class FakeGrouper:
        async def group(self, selected, rows):
            assert selected.goal_id == goal.goal_id
            assert [r["analysis_id"] for r in rows] == [analysis.analysis_id]
            calls.append(True)
            budget.record(10, 5, stage="dedup")
            if fail:
                raise ValueError("invalid model response")
            return [Group(members=[analysis.analysis_id], overview="gift")]

    budget = None

    def build(settings, **kwargs):
        nonlocal budget
        budget = kwargs["budget"]
        return FakeGrouper()

    monkeypatch.setattr("crawlme.cli.dedup.Settings", lambda: Settings(_env_file=None, result_dir=tmp_path))
    monkeypatch.setattr("crawlme.cli.dedup.Grouper.from_settings", build)
    monkeypatch.setattr("crawlme.cli.dedup.close_litellm_clients", AsyncMock())
    args = argparse.Namespace(
        task_id=task.task_id, goal=goal.goal_id, result_dir=str(tmp_path), log_level="WARNING", max_tokens=100
    )
    if fail:
        with pytest.raises(SystemExit, match="1"):
            await cmd_dedup(args)
    else:
        await cmd_dedup(args)
    assert calls == [True]
    out = capsys.readouterr().out
    assert "10 in / 5 out" in out
    assert out.strip() in (tmp_path / storage.raw_dir.parent.name / "log").read_text()
    with sqlite3.connect(storage.db_path) as con:
        assert con.execute("SELECT count(*) FROM analyses").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM dedup_runs").fetchone()[0] == (1 if fail else 2)
        assert con.execute("SELECT goal_id FROM crawl_tasks").fetchone()[0] == other.goal_id


async def test_missing_task_returns_failure_without_llm(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("crawlme.cli.dedup.Settings", lambda: Settings(_env_file=None, result_dir=tmp_path))
    factory = AsyncMock()
    monkeypatch.setattr("crawlme.cli.dedup.Grouper.from_settings", factory)
    monkeypatch.setattr("crawlme.cli.dedup.close_litellm_clients", AsyncMock())
    args = argparse.Namespace(task_id="missing", goal=None, result_dir=None, log_level="OFF", max_tokens=None)
    with pytest.raises(SystemExit):
        await cmd_dedup(args)
    factory.assert_not_called()
    assert "0 calls" in capsys.readouterr().out
