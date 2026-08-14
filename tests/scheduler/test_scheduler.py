"""Unit tests for CrawlScheduler (mock all I/O, verify control flow)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.scheduler.engine import CrawlScheduler
from crawlme.schemas import URL, CrawlGoal, CrawlTask, FrontierItem
from crawlme.state.context import CrawlCounters


def _goal(**kw) -> CrawlGoal:
    defaults: dict = dict(prompt="test", max_pages=5)
    defaults.update(kw)
    return CrawlGoal(**defaults)


def _task() -> CrawlTask:
    return CrawlTask(task_id="t1", state="CREATED")  # type: ignore[arg-type]


def _item() -> FrontierItem:
    url = URL(raw="https://example.com", canonical="https://example.com", url_key="k1", reg_domain="example.com")
    return FrontierItem(url=url, url_key="k1", priority=0.5, depth=0, reg_domain="example.com")


def _make_sched(**overrides) -> CrawlScheduler:
    """Build a scheduler with all-mock components for unit tests."""
    from crawlme.config import Settings

    buffer_mock = MagicMock()
    buffer_mock.wake = AsyncMock()
    buffer_mock.wait_until = AsyncMock()

    kwargs: dict = {
        "settings": Settings(),
        "storage": MagicMock(),
        "frontier": MagicMock(),
        "fetcher": MagicMock(),
        "extractor": MagicMock(),
        "robots": MagicMock(),
        "prefilter": MagicMock(),
        "buffer": buffer_mock,
        "ranker": MagicMock(aclose=AsyncMock()),
        "canonicalizer": MagicMock(),
    }
    kwargs.update(overrides)
    return CrawlScheduler(**kwargs)  # type: ignore[arg-type]


def test_note_tokens_used_updates_counters():
    """The TokenBudget sink lands in the shared counters, which the
    BUDGET_TOKENS stop condition reads every pump iteration."""
    sched = _make_sched()
    sched.note_tokens_used(1234)
    assert sched._counters.tokens_used == 1234


@pytest.mark.asyncio
async def test_stops_when_frontier_empty():
    """Scheduler should stop immediately when frontier is empty and buffer empty."""
    sched = _make_sched()

    sched._state = "RUNNING"
    sched._goal = _goal(max_pages=5)
    sched._task = _task()
    sched._counters = CrawlCounters(
        max_pages=5,
        max_tokens=100000,
        max_duration_sec=3600,
        min_relevant_hits=3,
        relevance_threshold=0.7,
    )

    # Mock frontier.pop_next to return None immediately.
    sched._frontier.pop_next = AsyncMock(return_value=None)

    await sched._fetch_pump()

    assert sched._state == "STOPPING"


@pytest.mark.asyncio
async def test_stops_on_budget_pages():
    """Scheduler should stop when pages_fetched reaches max_pages."""
    sched = _make_sched()

    sched._state = "RUNNING"
    sched._goal = _goal(max_pages=10)
    sched._task = _task()
    sched._counters = CrawlCounters(
        max_pages=10,
        pages_fetched=10,  # Already at budget.
        max_tokens=100000,
        max_duration_sec=3600,
        min_relevant_hits=3,
        relevance_threshold=0.7,
    )

    await sched._fetch_pump()

    # BUDGET_PAGES fires immediately, so it never reaches pop_next.
    assert sched._state == "STOPPING"


@pytest.mark.asyncio
async def test_budget_gate_blocks_pops_while_inflight():
    """Committed budget (fetched + in-flight) must block new pops.

    Regression: the pump used to keep popping while fetches were in
    the air, overshooting max_pages by up to fetch_concurrency-1.
    """
    sched = _make_sched()
    sched._state = "RUNNING"
    sched._goal = _goal(max_pages=10)
    sched._task = _task()
    sched._counters = CrawlCounters(
        max_pages=10,
        pages_fetched=8,
        in_flight=2,  # 8 + 2 = 10 committed: nothing may be popped
    )

    pop_mock = AsyncMock(return_value=None)
    sched._frontier.pop_next = pop_mock

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sched._fetch_pump(), timeout=0.5)

    pop_mock.assert_not_called()


@pytest.mark.asyncio
async def test_budget_gate_allows_pops_under_budget():
    """Below budget, pops still happen (gate is a cap, not a stall)."""
    sched = _make_sched()
    sched._state = "RUNNING"
    sched._goal = _goal(max_pages=10)
    sched._task = _task()
    sched._counters = CrawlCounters(
        max_pages=10,
        pages_fetched=8,
        in_flight=1,  # 9 committed < 10: one more pop is allowed
    )

    pop_mock = AsyncMock(return_value=None)
    sched._frontier.pop_next = pop_mock
    sched._frontier.size = 0
    sched._buffer.is_empty = True

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sched._fetch_pump(), timeout=0.5)

    pop_mock.assert_called()


@pytest.mark.asyncio
async def test_rank_pump_exits_when_stopping():
    """Rank pump should exit when state transitions away from RUNNING."""
    sched = _make_sched()
    sched._state = "STOPPING"
    sched._goal = _goal()
    sched._counters = CrawlCounters()

    await sched._rank_pump()
    # Should exit immediately without error.
    assert sched._state == "STOPPING"


@pytest.mark.asyncio
async def test_pause_sets_state():
    """pause() should set state to PAUSED after in-flight tasks finish."""
    sched = _make_sched()
    sched._state = "RUNNING"
    sched._task = _task()
    sched._counters = CrawlCounters()

    # Mock checkpoint to avoid storage calls.
    sched._checkpoint = AsyncMock()

    await sched.pause()

    assert sched._state == "PAUSED"
    assert sched._task.state == "PAUSED"


@pytest.mark.asyncio
async def test_stop_sets_stopping():
    """stop() should set state to STOPPING."""
    sched = _make_sched()
    sched._state = "RUNNING"
    sched._task = _task()

    await sched.stop()

    assert sched._state == "STOPPING"
    assert sched._task.state == "STOPPING"


@pytest.mark.asyncio
async def test_aclose_closes_analyzer_and_ranker():
    """Shutdown must release stage-held resources (drain tasks, caches)."""
    analyzer = MagicMock(aclose=AsyncMock())
    ranker = MagicMock(aclose=AsyncMock())
    storage = MagicMock(close=AsyncMock())
    sched = _make_sched(analyzer=analyzer, ranker=ranker, storage=storage)
    await sched.aclose()
    analyzer.aclose.assert_awaited_once()
    ranker.aclose.assert_awaited_once()
    storage.close.assert_awaited_once()


def test_summary_reports_run_statistics():
    """summary() reads counters and stats straight from the context."""
    sched = _make_sched()
    sched._counters = CrawlCounters(pages_fetched=12, tokens_used=5000, started_at=100.0)
    sched._ctx.stats.links_discovered = 123
    sched._ctx.stats.candidates_ranked = 45
    sched._ctx.stats.fetch_errors = 2
    sched._ctx.stats.analyses_by_class = {"RELEVANT": 3, "IRRELEVANT": 1}
    sched._ctx.stats.embedding_cache_hits = 4
    sched._ctx.stats.embedding_cache_misses = 9

    summary = sched.summary()

    assert summary["pages_fetched"] == 12
    assert summary["tokens_used"] == 5000
    assert summary["candidates_discovered"] == 123
    assert summary["candidates_ranked"] == 45
    assert summary["fetch_errors"] == 2
    assert summary["analyses"] == {"RELEVANT": 3, "IRRELEVANT": 1}
    assert summary["embedding_cache_hits"] == 4
    assert summary["embedding_cache_misses"] == 9
