"""Recover stored page states in a fresh scheduler without draining on cancel."""

import asyncio
import copy
import datetime
import json
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.analysis import Analyzer
from crawlme.checkpoint import VERSION, Checkpointer
from crawlme.config import Settings
from crawlme.errors import Kind
from crawlme.llm import LLMResponse, RemoteError, TokenBudget
from crawlme.persist import Lock, Storage
from crawlme.pioneer import RankDecision
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.runtime import Task
from crawlme.scheduler.pipelines.crawl import CrawlInput
from tests.scheduler.test_control import build, candidate


def snapshot(scheduler):
    with sqlite3.connect(scheduler.pipeline._storage.db_path) as db:
        return json.loads(db.execute("SELECT checkpoint_json FROM checkpoints").fetchone()[0])


def reopen(scheduler):
    return Storage(scheduler.pipeline._storage.db_path, str(scheduler.pipeline._storage.raw_dir))


@pytest.mark.parametrize("version", [None, VERSION - 1, VERSION + 1, str(VERSION), float(VERSION), True])
async def test_restore_rejects_version_before_accessing_state(version):
    storage = MagicMock()
    runtime = MagicMock()
    checkpointer = Checkpointer(
        storage=storage,
        settings=Settings(),
        frontier=MagicMock(),
        runtime=runtime,
        analysis=None,
        budget=None,
        robots=MagicMock(),
        features={},
    )
    payload = {} if version is None else {"version": version}
    with pytest.raises(ValueError, match="unsupported checkpoint version"):
        await checkpointer.restore(payload)
    runtime.restore.assert_not_called()
    storage.get_page.assert_not_called()


@pytest.mark.parametrize("stage", ["dispatch", "fetch", "analysis", "applied", "discovery", "ranking", "retry"])
async def test_resume_interrupted_stage(tmp_path, monkeypatch, stage):
    monkeypatch.setattr("crawlme.scheduler.engine.engine._CHECKPOINT_INTERVAL", 0.01)
    entered = asyncio.Event()
    calls = []
    budget = TokenBudget(100)

    async def chat(*args, **kwargs):
        calls.append("analysis")
        if stage == "retry" and calls.count("analysis") == 1:
            raise RemoteError("retry", kind=Kind.UNAVAILABLE)
        if stage in {"analysis", "retry"}:
            entered.set()
            await asyncio.Event().wait()
        budget.record(2, 1, stage="analysis")
        return LLMResponse('{"classification":"RELEVANT","relevance_score":0.9}', 2, 1, "stub")

    analyzer = Analyzer(MagicMock(chat=chat))
    scheduler, goal, task, fetched = build(tmp_path, analyzer=analyzer, budget=budget)

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    if stage == "dispatch":
        scheduler.pipeline._page.run = blocked
    if stage == "fetch":
        scheduler.pipeline._page._fetch.fetcher.fetch = blocked
    if stage == "applied":
        analyze = scheduler.pipeline._page._analysis.analyze

        async def after_apply(*args, **kwargs):
            await analyze(*args, **kwargs)
            await blocked()

        scheduler.pipeline._page._analysis.analyze = after_apply
    if stage == "discovery":
        scheduler.pipeline._page._discovery.discover = blocked
    if stage == "ranking":
        scheduler.pipeline._ranking.ranker = MagicMock(rank_batch=blocked, aclose=AsyncMock())
        await scheduler.pipeline._frontier.push_candidates([candidate()])
    else:
        await scheduler.pipeline.ingest_seeds(goal, [candidate("seed")])
    run = asyncio.create_task(scheduler.engine.run(CrawlInput(goal, task)))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await scheduler.engine._checkpoint()
        saved = snapshot(scheduler)
        pending = scheduler.pipeline._page.pending_items()
        before = len(calls)
    finally:
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, 1)
    assert saved["version"] == VERSION
    assert "work" not in saved
    assert "llm_api_key" not in saved["settings"]
    assert "page_states" in scheduler.pipeline.run_state.dump()
    assert "page_states" not in saved
    assert "work" not in saved["state"]
    if stage not in {"ranking", "retry"}:
        assert len(saved["state"]["page_states"]) == len(pending) == 1
        assert bool(saved["state"]["page_states"][0].get("analysis_processed")) == (stage in {"applied", "discovery"})
    if stage == "retry":
        assert len(saved["retries"]) == 1
        assert all(entry["analysis_processed"] for entry in saved["state"]["page_states"])
    for entry in [*saved["state"]["page_states"], *saved["retries"]]:
        assert "plain_text" not in entry.get("page", {})
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
        analyzer=Analyzer(MagicMock(chat=complete)),
        ranker=MagicMock(rank_batch=rank, aclose=AsyncMock()),
        budget=TokenBudget(100),
    )
    original_snapshot = copy.deepcopy(saved)
    restored_task = Task.model_validate(saved["task"])
    await asyncio.wait_for(fresh.engine.run(CrawlInput(goal, restored_task, checkpoint=saved)), 3)
    assert saved == original_snapshot
    assert restored_task.state == "COMPLETED"
    assert fresh.pipeline.run_state.progress.pages_fetched == 1
    assert fresh.pipeline.run_state.stats.analyses_by_class == {"RELEVANT": 1}
    assert len(resumed_calls) == (0 if stage in {"applied", "discovery"} else 1)
    assert len(fetched) == (1 if stage in {"dispatch", "fetch", "ranking"} else 0)
    assert fresh.pipeline._checkpointer.budget.used == (3 if stage in {"applied", "discovery"} else 0)
    assert not fresh.pipeline._page.pending_items()
    latest = snapshot(fresh)
    assert latest["version"] == VERSION
    assert "page_states" in latest["state"] and "work" not in latest["state"]


async def test_resume_older_snapshot(tmp_path):
    entered = asyncio.Event()

    async def block(page, depth):
        entered.set()
        await asyncio.Event().wait()

    response = LLMResponse('{"classification":"RELEVANT","relevance_score":0.9}', 0, 0, "stub")
    first, goal, task, _ = build(tmp_path, analyzer=Analyzer(MagicMock(chat=AsyncMock(return_value=response))))
    first.pipeline._page._discovery.discover = block
    await first.pipeline.ingest_seeds(goal, [candidate()])
    # Keep the initial checkpoint; allow results to advance ahead of it.
    original = first.engine._save
    saved = None

    async def once():
        nonlocal saved
        if saved is None:
            await original()
            saved = snapshot(first)

    first.engine._save = once
    run = asyncio.create_task(first.engine.run(CrawlInput(goal, task)))
    await asyncio.wait_for(entered.wait(), 3)
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    fresh, _, _, _ = build(
        tmp_path, storage=reopen(first), analyzer=Analyzer(MagicMock(chat=AsyncMock(return_value=response)))
    )
    await fresh.engine.run(CrawlInput(goal, Task.model_validate(saved["task"]), checkpoint=saved))
    with sqlite3.connect(first.pipeline._storage.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 1
    assert fresh.pipeline.run_state.stats.analyses_by_class == {"RELEVANT": 1}


def test_run_lock(tmp_path):
    first = Lock(str(tmp_path / "crawl.db"))
    second = Lock(str(tmp_path / "crawl.db"))
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
    first.pipeline._robots.record_response("example.com", 429)
    await first.pipeline.ingest_seeds(goal, [candidate()])
    run = asyncio.create_task(first.engine.run(CrawlInput(goal, task)))
    await asyncio.wait_for(entered.wait(), 2)
    await first.engine._checkpoint()
    saved = snapshot(first)
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    saved["state"]["progress"]["started_at"] = 12.0
    saved["task"]["start_at"] = "2000-01-01T00:00:00+00:00"
    fresh, _, _, _ = build(tmp_path, storage=reopen(first), budget=TokenBudget(10))
    await fresh.engine.run(CrawlInput(goal, Task.model_validate(saved["task"]), checkpoint=saved))
    assert 12 <= time.monotonic() - fresh.pipeline.run_state.progress.started_at < 15
    assert fresh.pipeline._checkpointer.budget.used == fresh.pipeline.summary()["tokens_used"] == 6
    assert fresh.pipeline._checkpointer.budget.by_stage["ranking"].calls == 1
    assert fresh.pipeline._robots.next_allowed_at("example.com") == datetime.datetime.fromisoformat(
        saved["robots"]["next_allowed"]["example.com"]
    )


@pytest.mark.parametrize("status", [402, 503])
@pytest.mark.parametrize("stage", ["analysis", "retry", "ranking", "dedup", "goal", "seeds"])
async def test_remote_resume(tmp_path, monkeypatch, stage, status):
    from types import SimpleNamespace

    import httpx

    from crawlme.dedup import Grouper
    from crawlme.llm import Client
    from crawlme.pioneer.goal_enhancer import GoalEnhancer
    from crawlme.pioneer.ranker import LLMRanker
    from crawlme.pioneer.seed_expander import SeedExpander

    calls = []
    message = "Insufficient credits" if status == 402 else "Service is too busy"
    provider_code = "payment_required" if status == 402 else "service_unavailable"
    content = json.dumps(
        {
            "classification": "RELEVANT",
            "relevance_score": 0.9,
            "items": [
                {
                    "summary": "Compiler correctness",
                    "relevance_score": 0.9,
                    "evidence": ["An article about compiler correctness."],
                }
            ],
        }
    )

    async def complete(client, *args, **kwargs):
        calls.append(client._stage)
        if stage == "retry" and len(calls) == 1:
            raise RemoteError("temporary failure", kind=Kind.UNAVAILABLE)
        if client._stage == stage or stage == "retry":
            response = httpx.Response(
                status,
                json={"error": {"message": message, "code": provider_code}},
                request=httpx.Request("POST", "https://provider.test"),
            )
            raise httpx.HTTPStatusError("Payment required", request=response.request, response=response)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0),
        )

    monkeypatch.setattr(Client, "_complete", complete)
    scheduler, goal, task, _ = build(
        tmp_path,
        analyzer=Analyzer(Client("provider/model", stage="analysis", reasoning_effort="off")),
        grouper=Grouper(Client("provider/model", stage="dedup", reasoning_effort="off"), max_chars=100000)
        if stage == "dedup"
        else None,
        ranker=LLMRanker(Client("provider/model", stage="ranking")) if stage == "ranking" else None,
    )
    if stage == "ranking":
        await scheduler.pipeline._frontier.push_candidates([candidate()])
    else:
        await scheduler.pipeline.ingest_seeds(goal, [candidate()])
        if stage == "dedup":
            await scheduler.pipeline.ingest_seeds(goal, [candidate("second")])
    client = Client("provider/model", stage=stage)
    if stage in {"goal", "seeds"}:
        scheduler.pipeline._goal_enhancer = GoalEnhancer(client) if stage == "goal" else GoalEnhancer(None)
        scheduler.pipeline._seed_expander = SeedExpander(client) if stage == "seeds" else None
        await scheduler.pipeline.startup(goal, [candidate()])
    await asyncio.wait_for(scheduler.engine.run(CrawlInput(goal, task)), 2)
    assert task.state == "FAILED" and task.stopping_reason == "FATAL"
    error = scheduler.pipeline.run_state.progress.fatal_error
    assert str(status) in error and message in error
    expected_calls = (
        (3 if stage == "dedup" else 2 if stage == "retry" else 1) if status == 402 else (5 if stage == "dedup" else 3)
    )
    assert len(calls) == expected_calls
    saved = snapshot(scheduler)
    # Cleanup must not replace recoverable state with the emptied retry queue.
    assert not saved["state"]["progress"]["fatal_error"]
    if stage == "retry":
        assert len(saved["retries"]) == 1
    if stage == "dedup":
        assert task.state == "FAILED"
    if stage == "analysis":
        if status == 402:
            assert len(saved["state"]["page_states"]) == 1 and not saved["state"]["page_states"][0].get(
                "analysis_processed"
            )
        else:
            assert len(saved["retries"]) == 1

    async def analyze(*args, **kwargs):
        return LLMResponse(content, 0, 0, "stub")

    async def rank(goal, batch, history, **kwargs):
        return [RankDecision(candidate_id=c.candidate_id, url_key=c.url.url_key, priority=1) for c in batch]

    fresh, _, _, _ = build(
        tmp_path,
        storage=reopen(scheduler),
        analyzer=Analyzer(MagicMock(chat=analyze)),
        ranker=MagicMock(rank_batch=rank, aclose=AsyncMock()),
    )
    restored_task = Task.model_validate(saved["task"])
    await asyncio.wait_for(fresh.engine.run(CrawlInput(goal, restored_task, checkpoint=saved)), 2)
    assert restored_task.state == "COMPLETED"
    assert fresh.pipeline.run_state.stats.analyses_by_class == {"RELEVANT": 2 if stage == "dedup" else 1}


async def test_remote_cancels_calls(tmp_path, monkeypatch):
    import httpx

    from crawlme.llm import Client

    entered, cancelled = asyncio.Event(), asyncio.Event()
    requests = 0

    async def complete(self, *args, **kwargs):
        nonlocal requests
        requests += 1
        if requests == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        await entered.wait()
        response = httpx.Response(402, request=httpx.Request("POST", "https://provider.test"))
        raise httpx.HTTPStatusError("Payment required", request=response.request, response=response)

    monkeypatch.setattr(Client, "_complete", complete)
    scheduler, goal, task, _ = build(tmp_path, analyzer=Analyzer(Client("provider/model", concurrency=2)))
    scheduler.pipeline._page._analysis._slots = asyncio.Semaphore(3)
    await scheduler.pipeline.ingest_seeds(goal, [candidate("first"), candidate("second"), candidate("third")])
    await asyncio.wait_for(scheduler.engine.run(CrawlInput(goal, task)), 2)
    assert task.state == "FAILED" and task.stopping_reason == "FATAL"
    assert cancelled.is_set() and requests == 2
    assert not scheduler.pipeline._inflight and not scheduler.pipeline._pump_tasks


async def test_failed_initial_checkpoint(tmp_path, caplog):
    scheduler, goal, task, _ = build(tmp_path)
    scheduler.pipeline._checkpointer.save = AsyncMock(side_effect=OSError("Disk full"))
    await scheduler.engine.run(CrawlInput(goal, task))
    assert task.state == "FAILED" and task.stopping_reason == "FATAL"
    assert "Disk full" in caplog.text and "crawl resume" not in caplog.text
    with sqlite3.connect(scheduler.pipeline._storage.db_path) as db:
        assert db.execute("SELECT checkpoint_json FROM checkpoints").fetchone() is None


@pytest.mark.parametrize("stop", ["remote", "user", "failure", "ranking"])
@pytest.mark.parametrize("source", ["stage", "periodic"])
async def test_stop_queued_checkpoint(tmp_path, monkeypatch, stop, source):
    import httpx

    from crawlme.llm import Client
    from tests.scheduler.test_control import wait_until

    entered, reject, closing, release = (asyncio.Event() for _ in range(4))

    async def complete(self, *args, **kwargs):
        entered.set()
        if stop == "ranking":
            await asyncio.Event().wait()
        await reject.wait()
        response = httpx.Response(402, request=httpx.Request("POST", "https://provider.test"))
        raise httpx.HTTPStatusError("Payment required", request=response.request, response=response)

    monkeypatch.setattr(Client, "_complete", complete)
    if source == "periodic":
        monkeypatch.setattr("crawlme.scheduler.engine.engine._CHECKPOINT_INTERVAL", 0.005)
    scheduler, goal, task, _ = build(tmp_path, analyzer=Analyzer(Client("provider/model")))
    if stop == "ranking":

        async def rank(*args, **kwargs):
            await reject.wait()
            raise RuntimeError("Ranking unavailable")

        scheduler.pipeline._ranking.ranker = MagicMock(rank_batch=rank, aclose=AsyncMock())
        await scheduler.pipeline._frontier.push_candidates([candidate("rank")])
    await scheduler.pipeline.ingest_seeds(goal, [candidate()])
    abort = scheduler.pipeline.abort

    async def held_abort():
        closing.set()
        await release.wait()
        await abort()

    scheduler.pipeline.abort = held_abort
    run = asyncio.create_task(scheduler.engine.run(CrawlInput(goal, task)))
    queued = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with scheduler.pipeline._checkpointer._lock:
            saved = snapshot(scheduler)
            queued = (
                scheduler.engine._checkpoint_task
                if source == "periodic"
                else asyncio.create_task(scheduler.engine._checkpoint())
            )
            await wait_until(lambda: scheduler.pipeline._checkpointer._lock._waiters)
            if stop in {"remote", "ranking"}:
                reject.set()
            elif stop == "failure":
                scheduler.pipeline.error_handler.abort(RuntimeError("Storage unavailable"), "persist")
            else:
                run.cancel()
            await asyncio.wait_for(closing.wait(), 2)
            # Cleanup starts only after even a lock-blocked save has stopped.
            assert queued.done()
            with pytest.raises(asyncio.CancelledError):
                await queued
        assert snapshot(scheduler) == saved
        release.set()
        if stop == "user":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(run, 2)
        else:
            await asyncio.wait_for(run, 2)
        assert task.state == ("INTERRUPTED" if stop == "user" else "FAILED")
        if stop == "failure":
            assert scheduler.pipeline.run_state.progress.fatal_error == "Storage unavailable"
    finally:
        release.set()
        run.cancel()
        if queued is not None:
            queued.cancel()
        await asyncio.gather(run, *([queued] if queued is not None else []), return_exceptions=True)


@pytest.mark.parametrize("stop", ["deadline", "tokens", "user"])
async def test_settle_freezes_before_abort(tmp_path, monkeypatch, stop):
    entered, settling = asyncio.Event(), asyncio.Event()
    cancelled_after_freeze = []

    async def fetch(item):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled_after_freeze.append(
                not scheduler.engine._checkpointing and not scheduler.engine._checkpoint_saves
            )

    scheduler, goal, task, _ = build(tmp_path, fetcher=MagicMock(fetch=fetch, aclose=AsyncMock()))
    goal.max_relevant = 1
    if stop == "deadline":
        monkeypatch.setattr("crawlme.scheduler.engine.engine._SETTLE_TIMEOUT", 0.05)
    settle_pages = scheduler.pipeline._settle_pages

    async def wait_for_pages():
        settling.set()
        await settle_pages()

    scheduler.pipeline._settle_pages = wait_for_pages
    await scheduler.pipeline.ingest_seeds(goal, [candidate()])
    running = asyncio.create_task(scheduler.engine.run(CrawlInput(goal, task)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        scheduler.pipeline.run_state.analysis_completed(True)
        await asyncio.wait_for(settling.wait(), 2)
        if stop == "tokens":
            scheduler.pipeline._checkpointer.budget.record(goal.max_tokens, 0)
        elif stop == "user":
            running.cancel()
        if stop == "user":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(running, 2)
        else:
            await asyncio.wait_for(running, 2)
        assert cancelled_after_freeze == [True]
        assert snapshot(scheduler) is not None
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
