"""Unit tests for CrawlScheduler — mock all I/O, verify control flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crawlme.scheduler.engine import CrawlScheduler
from crawlme.schemas import URL, CrawlGoal, CrawlTask, FrontierItem


def _goal(**kw) -> CrawlGoal:
    defaults: dict = dict(prompt="test", max_pages=5)
    defaults.update(kw)
    return CrawlGoal(**defaults)


def _task() -> CrawlTask:
    return CrawlTask(task_id="t1", state="CREATED")  # type: ignore[arg-type]


def _item() -> FrontierItem:
    url = URL(raw="https://example.com", canonical="https://example.com", url_key="k1", reg_domain="example.com")
    return FrontierItem(url=url, url_key="k1", priority=0.5, depth=0, reg_domain="example.com")


@pytest.mark.asyncio
async def test_stops_when_frontier_empty():
    """Scheduler should stop immediately when frontier is empty and buffer empty."""
    with patch("crawlme.scheduler.engine.Storage") as mock_storage_cls:
        mock_storage = MagicMock()
        mock_storage.start = AsyncMock()
        mock_storage.close = AsyncMock()
        mock_storage_cls.return_value = mock_storage

        sched = CrawlScheduler(storage=mock_storage)
        # Don't start storage (we use mock)
        sched._storage = mock_storage

        goal = _goal(max_pages=5)
        task = _task()

        # Override run to avoid storage.start/close for this test.
        sched._state = "RUNNING"
        sched._goal = goal
        sched._task = task
        sched._counters = {
            "max_pages": 5,
            "pages_fetched": 0,
            "tokens_used": 0,
            "started_at": 0,
            "in_flight": 0,
            "max_tokens": 100000,
            "max_duration_sec": 3600,
            "min_relevant_hits": 3,
            "relevance_threshold": 0.7,
            "relevance_window": [],
            "fatal_error": "",
        }

        # Mock frontier.pop_next to return None immediately.
        sched._frontier.pop_next = AsyncMock(return_value=None)

        await sched._fetch_pump()

        assert sched._state == "STOPPING"


@pytest.mark.asyncio
async def test_stops_on_budget_pages():
    """Scheduler should stop when pages_fetched reaches max_pages."""
    with patch("crawlme.scheduler.engine.Storage") as mock_storage_cls:
        mock_storage = MagicMock()
        mock_storage.start = AsyncMock()
        mock_storage.close = AsyncMock()
        mock_storage_cls.return_value = mock_storage

        sched = CrawlScheduler(storage=mock_storage)
        sched._storage = mock_storage

        goal = _goal(max_pages=0)  # Already at budget.
        task = _task()

        sched._state = "RUNNING"
        sched._goal = goal
        sched._task = task
        sched._counters = {
            "max_pages": 0,
            "pages_fetched": 0,
            "tokens_used": 0,
            "started_at": 0,
            "in_flight": 0,
            "max_tokens": 100000,
            "max_duration_sec": 3600,
            "min_relevant_hits": 3,
            "relevance_threshold": 0.7,
            "relevance_window": [],
            "fatal_error": "",
        }

        sched._frontier.pop_next = AsyncMock(return_value=_item())

        await sched._fetch_pump()

        # BUDGET_PAGES should fire immediately (max_pages=0, pages_fetched=0).
        assert sched._state == "STOPPING"


@pytest.mark.asyncio
async def test_rank_pump_exits_when_stopping():
    """Rank pump should exit when state transitions away from RUNNING."""
    sched = CrawlScheduler()
    sched._state = "STOPPING"
    sched._goal = _goal()
    sched._counters = {"pages_fetched": 0}

    await sched._rank_pump()
    # Should exit immediately without error.
    assert sched._state == "STOPPING"


@pytest.mark.asyncio
async def test_pause_sets_state():
    """pause() should set state to PAUSED after in-flight tasks finish."""
    sched = CrawlScheduler()
    sched._state = "RUNNING"
    sched._task = _task()
    sched._counters = {"in_flight": 0}

    # Mock checkpoint to avoid storage calls.
    sched._checkpoint = AsyncMock()

    await sched.pause()

    assert sched._state == "PAUSED"
    assert sched._task.state == "PAUSED"


@pytest.mark.asyncio
async def test_stop_sets_stopping():
    """stop() should set state to STOPPING."""
    sched = CrawlScheduler()
    sched._state = "RUNNING"
    sched._task = _task()

    await sched.stop()

    assert sched._state == "STOPPING"
    assert sched._task.state == "STOPPING"
