"""Recover real stored work in a fresh scheduler without draining on cancel."""

import asyncio
import copy
import datetime
import json
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.analysis import PageAnalyzer
from crawlme.llm import LLMError, LLMResponse, TokenBudget
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.schemas import CrawlTask, RankDecision
from crawlme.storage.lock import RunLock
from crawlme.storage.sqlite import SqliteStorage
from tests.scheduler.test_control import build, candidate


def snapshot(scheduler):
    with sqlite3.connect(scheduler._storage.db_path) as db:
        return json.loads(db.execute("SELECT checkpoint_json FROM checkpoints").fetchone()[0])


def reopen(scheduler):
    return SqliteStorage(scheduler._storage.db_path, str(scheduler._storage.raw_dir))


@pytest.mark.parametrize("stage", ["fetch", "analysis", "discovery", "ranking", "retry"])
async def test_resume_interrupted_stage(tmp_path, monkeypatch, stage):
    monkeypatch.setattr("crawlme.scheduler.engine._CHECKPOINT_INTERVAL", 0.01)
    entered = asyncio.Event()
    calls = []
    budget = TokenBudget(100)

    async def chat(*args, **kwargs):
        calls.append("analysis")
        if stage == "retry" and calls.count("analysis") == 1:
            raise LLMError("retry")
        if stage in {"analysis", "retry"}:
            entered.set()
            await asyncio.Event().wait()
        budget.record(2, 1, stage="analysis")
        return LLMResponse('{"classification":"RELEVANT","relevance_score":0.9}', 2, 1, "stub")

    analyzer = PageAnalyzer(MagicMock(chat=chat), retry_delay=0)
    scheduler, goal, task, fetched = build(tmp_path, analyzer=analyzer, budget=budget)
    budget.bind_sink(scheduler.note_tokens_used)

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    if stage == "fetch":
        scheduler._fetch.fetcher.fetch = blocked
    if stage == "discovery":
        scheduler._discovery.discover = blocked
    if stage == "ranking":
        scheduler._ranking.ranker = MagicMock(rank_batch=blocked, aclose=AsyncMock())
        await scheduler._frontier.push_candidates([candidate()])
    else:
        await scheduler.ingest_seeds(goal, [candidate("seed")])
    run = asyncio.create_task(scheduler.run(goal, task))
    await asyncio.wait_for(entered.wait(), 3)
    await scheduler._checkpoint()
    saved = snapshot(scheduler)
    assert saved["version"] == 1
    assert "llm_api_key" not in saved["settings"]
    for work in [*saved["work"], *saved["retries"]]:
        assert "plain_text" not in work.get("page", {})
    before = len(calls)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 1)
    assert len(calls) == before
    assert task.state == "INTERRUPTED"

    resumed_calls = []

    async def complete(*args, **kwargs):
        resumed_calls.append("analysis")
        return LLMResponse('{"classification":"RELEVANT","relevance_score":0.9}', 2, 1, "stub")

    async def rank(goal, batch, history, **kwargs):
        return [RankDecision(candidate_id=c.candidate_id, url_key=c.url.url_key, priority=1) for c in batch]

    fresh, _, _, fetched = build(
        tmp_path,
        storage=reopen(scheduler),
        analyzer=PageAnalyzer(MagicMock(chat=complete), retry_delay=0),
        ranker=MagicMock(rank_batch=rank, aclose=AsyncMock()),
        budget=TokenBudget(100),
    )
    original_snapshot = copy.deepcopy(saved)
    restored_task = CrawlTask.model_validate(saved["task"])
    await asyncio.wait_for(fresh.run(goal, restored_task, checkpoint=saved), 3)
    assert saved == original_snapshot
    assert restored_task.state == "COMPLETED"
    assert fresh.run_state.progress.pages_fetched == 1
    assert fresh.run_state.stats.analyses_by_class == {"RELEVANT": 1}
    assert len(resumed_calls) == (0 if stage == "discovery" else 1)
    assert len(fetched) == (1 if stage in {"fetch", "ranking"} else 0)
    assert fresh._budget.used == (3 if stage == "discovery" else 0)


async def test_resume_older_snapshot(tmp_path):
    entered = asyncio.Event()

    async def block(page, depth):
        entered.set()
        await asyncio.Event().wait()

    response = LLMResponse('{"classification":"RELEVANT","relevance_score":0.9}', 0, 0, "stub")
    first, goal, task, _ = build(tmp_path, analyzer=PageAnalyzer(MagicMock(chat=AsyncMock(return_value=response))))
    first._discovery.discover = block
    await first.ingest_seeds(goal, [candidate()])
    # Keep the initial checkpoint; allow results to advance ahead of it.
    original = first._checkpoint
    saved = None

    async def once():
        nonlocal saved
        if saved is None:
            await original()
            saved = snapshot(first)

    first._checkpoint = once
    run = asyncio.create_task(first.run(goal, task))
    await asyncio.wait_for(entered.wait(), 3)
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    fresh, _, _, _ = build(
        tmp_path, storage=reopen(first), analyzer=PageAnalyzer(MagicMock(chat=AsyncMock(return_value=response)))
    )
    await fresh.run(goal, CrawlTask.model_validate(saved["task"]), checkpoint=saved)
    with sqlite3.connect(first._storage.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 1
    assert fresh.run_state.stats.analyses_by_class == {"RELEVANT": 1}


def test_run_lock(tmp_path):
    first = RunLock(str(tmp_path / "crawl.db"))
    second = RunLock(str(tmp_path / "crawl.db"))
    first.acquire()
    try:
        with pytest.raises(ValueError, match="already active"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


async def test_recovery_keeps_limits(tmp_path):
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    budget = TokenBudget(10)
    budget.record(4, 2, stage="ranking")
    first, goal, task, _ = build(
        tmp_path,
        budget=budget,
        robots=RobotsPolicy(circuit_threshold=1),
        fetcher=MagicMock(fetch=blocked, aclose=AsyncMock()),
    )
    first.note_tokens_used(budget.used)
    first._robots.record_response("example.com", 429)
    await first.ingest_seeds(goal, [candidate()])
    run = asyncio.create_task(first.run(goal, task))
    await asyncio.wait_for(entered.wait(), 2)
    await first._checkpoint()
    saved = snapshot(first)
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    saved["state"]["progress"]["started_at"] = 12.0
    saved["task"]["start_at"] = "2000-01-01T00:00:00+00:00"
    fresh, _, _, _ = build(tmp_path, storage=reopen(first), budget=TokenBudget(10))
    await fresh.run(goal, CrawlTask.model_validate(saved["task"]), checkpoint=saved)
    assert 12 <= time.monotonic() - fresh.run_state.progress.started_at < 15
    assert fresh._budget.used == fresh.run_state.progress.tokens_used == 6
    assert fresh._budget.by_stage["ranking"].calls == 1
    assert fresh._robots.next_allowed_at("example.com") == datetime.datetime.fromisoformat(
        saved["robots"]["next_allowed"]["example.com"]
    )
