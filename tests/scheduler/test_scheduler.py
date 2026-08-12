"""Unit tests for CrawlScheduler — mock all I/O, verify control flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.scheduler.engine import CrawlScheduler
from crawlme.schemas import URL, CrawlCounters, CrawlGoal, CrawlTask, FrontierItem


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
        "ranker": MagicMock(),
        "canonicalizer": MagicMock(),
    }
    kwargs.update(overrides)
    return CrawlScheduler(**kwargs)  # type: ignore[arg-type]


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

    # BUDGET_PAGES fires immediately — never reaches pop_next.
    assert sched._state == "STOPPING"


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
