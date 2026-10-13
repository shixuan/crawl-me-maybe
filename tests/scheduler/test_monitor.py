"""Stop monitoring reacts to runtime updates and actual deadlines."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.analysis import Analyzer
from crawlme.discovery import Harvest
from crawlme.llm import Error, LLMResponse
from crawlme.pioneer.frontier import Frontier
from tests.scheduler.test_control import build, candidate, wait_until


async def test_idle_then_token_wakeup(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(item):
        entered.set()
        await release.wait()
        return await original_fetch(item)

    scheduler, goal, task, _ = build(tmp_path)
    original_fetch = scheduler.pipeline._page._fetch.fetcher.fetch
    scheduler.pipeline._page._fetch.fetcher.fetch = fetch
    wait = scheduler._wait_for_stop
    waits = []

    async def counted(pending, **kwargs):
        waits.append(kwargs)
        return await wait(pending, **kwargs)

    scheduler._wait_for_stop = counted
    await scheduler.pipeline.ingest_seeds(goal, [candidate()])
    running = asyncio.create_task(scheduler.run(goal, task))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0.05)
        before = len(waits)
        for _ in range(5):
            scheduler._checkpointer.budget.record(1, 0)
            await asyncio.sleep(0.01)
            assert task.state == "RUNNING"
            assert len(waits) == before
        await asyncio.sleep(0.4)
        assert len(waits) == before
        scheduler._checkpointer.budget.record(goal.max_tokens, 0)
        await wait_until(lambda: task.state == "STOPPING")
        assert task.stopping_reason == "BUDGET_TOKENS"
        release.set()
        await asyncio.wait_for(running, 2)
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_notice_before_wait(tmp_path):
    scheduler, goal, task, _ = build(tmp_path)
    wait = scheduler._monitor._wait_for_update
    injected = False

    async def notify_then_wait(revision):
        nonlocal injected
        if not injected:
            injected = True
            scheduler._checkpointer.budget.record(goal.max_tokens, 0)
        await wait(revision)

    scheduler._monitor._wait_for_update = notify_then_wait
    await scheduler.pipeline.ingest_seeds(goal, [candidate()])
    await asyncio.wait_for(scheduler.run(goal, task), 2)
    assert injected
    assert task.stopping_reason == "BUDGET_TOKENS"


async def test_last_candidate_refused(tmp_path):
    scheduler, goal, task, fetched = build(
        tmp_path,
        frontier=Frontier(domain_budget=1),
        harvester=MagicMock(harvest=lambda page, depth: Harvest([candidate("next")])),
    )
    await scheduler.pipeline.ingest_seeds(goal, [candidate("seed")])
    await asyncio.wait_for(scheduler.run(goal, task), 3)
    assert len(fetched) == 1
    assert "DOMAIN_BUDGET" in task.stopping_reason
    assert "FRONTIER_DRAINED" in task.stopping_reason


async def test_target_before_page_end(tmp_path):
    release = asyncio.Event()
    response = LLMResponse('{"classification":"RELEVANT","relevance_score":0.9}', 1, 1, "stub")
    scheduler, goal, task, _ = build(tmp_path, analyzer=Analyzer(MagicMock(chat=AsyncMock(return_value=response))))
    goal.max_relevant = 1

    async def discover(page, depth):
        await release.wait()
        return Harvest([])

    scheduler.pipeline._page._discovery.discover = discover
    await scheduler.pipeline.ingest_seeds(goal, [candidate()])
    running = asyncio.create_task(scheduler.run(goal, task))
    try:
        await wait_until(lambda: task.state == "STOPPING")
        assert task.stopping_reason == "MAX_RELEVANT"
        assert scheduler.run_state.progress.in_flight == 1
        release.set()
        await asyncio.wait_for(running, 2)
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("stage", ["fetch", "retry"])
async def test_deadline_without_changes(tmp_path, stage):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def chat(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Error("retry")
        entered.set()
        await release.wait()
        return LLMResponse('{"classification":"RELEVANT","relevance_score":0.9}', 1, 1, "stub")

    scheduler, goal, task, _ = build(
        tmp_path, analyzer=Analyzer(MagicMock(chat=chat), retry_delay=0) if stage == "retry" else None
    )
    goal.max_duration_sec = 1
    original_fetch = scheduler.pipeline._page._fetch.fetcher.fetch

    async def fetch(item):
        entered.set()
        await release.wait()
        return await original_fetch(item)

    if stage == "fetch":
        scheduler.pipeline._page._fetch.fetcher.fetch = fetch
    await scheduler.pipeline.ingest_seeds(goal, [candidate()])
    running = asyncio.create_task(scheduler.run(goal, task))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if stage == "fetch":
            await wait_until(lambda: task.stopping_reason == "BUDGET_TIME")
            release.set()
        await asyncio.wait_for(running, 2)
        assert task.state == "COMPLETED"
        assert scheduler.summary()["tokens_used"] == 0
        if stage == "retry":
            assert calls == 2
            assert not scheduler.run_state.stats.analyses_by_class
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_hard_limit_during_settle(tmp_path):
    entered = asyncio.Event()

    async def discover(page, depth):
        entered.set()
        await asyncio.Event().wait()

    response = LLMResponse('{"classification":"RELEVANT","relevance_score":0.9}', 1, 1, "stub")
    analyzer = Analyzer(MagicMock(chat=AsyncMock(return_value=response)))
    scheduler, goal, task, _ = build(tmp_path, analyzer=analyzer)
    goal.max_relevant = 1
    scheduler.pipeline._page._discovery.discover = discover
    await scheduler.pipeline.ingest_seeds(goal, [candidate()])
    running = asyncio.create_task(scheduler.run(goal, task))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await wait_until(lambda: task.state == "STOPPING")
        assert task.stopping_reason == "MAX_RELEVANT"
        assert scheduler.run_state.progress.in_flight == 1
        scheduler._checkpointer.budget.record(goal.max_tokens, 0)
        await asyncio.wait_for(running, 2)
        assert task.state == "COMPLETED"
        assert scheduler.run_state.progress.in_flight == 0
        assert not scheduler.pipeline._inflight
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_observed_owners(tmp_path):
    scheduler, goal, _, _ = build(tmp_path)
    state = scheduler.run_state
    state.reset(goal=goal)
    revision = scheduler._monitor._revision
    await scheduler.pipeline.ingest_seeds(goal, [candidate()])
    await asyncio.wait_for(scheduler._monitor._wait_for_update(revision), 1)
    assert not scheduler._monitor.reasons()
    revision = scheduler._monitor._revision
    scheduler._checkpointer.budget.record(goal.max_tokens, 0)
    await asyncio.wait_for(scheduler._monitor._wait_for_update(revision), 1)
    assert [reason.code for reason in scheduler._monitor.reasons()] == ["BUDGET_TOKENS"]
    assert scheduler.summary()["tokens_used"] == goal.max_tokens
    assert "tokens_used" not in state.dump()["progress"]


async def test_unchanged_data_is_quiet(tmp_path):
    scheduler, _, _, _ = build(tmp_path)
    state = scheduler.run_state
    frontier = scheduler.pipeline._frontier
    revision = scheduler._monitor._revision
    await frontier.pop_next()
    await frontier.push_candidates([])
    frontier.finish_ranking([])
    state.analysis_completed(False)
    state.stats.fetch_errors += 1
    scheduler._checkpointer.budget.record(0, 0)
    assert scheduler._monitor._revision == revision


async def test_monitor_after_state_restore(tmp_path):
    scheduler, goal, _, _ = build(tmp_path)
    state = scheduler.run_state
    budget = scheduler._checkpointer.budget
    await scheduler.pipeline.ingest_seeds(goal, [candidate()])
    budget.record(4, 2)
    state.reset(goal=goal)
    saved = state.dump()
    state.restore(saved)
    revision = scheduler._monitor._revision
    budget.record(goal.max_tokens, 0)
    await asyncio.wait_for(scheduler._monitor._wait_for_update(revision), 1)
    assert [reason.code for reason in scheduler._monitor.reasons()] == ["BUDGET_TOKENS"]
    assert scheduler.summary()["tokens_used"] == goal.max_tokens + 6
    assert "tokens_used" not in saved["progress"]
