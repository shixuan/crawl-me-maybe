"""Unit tests for Engine (mock all I/O, verify control flow)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.scheduler.helpers import (
    _goal,
    _make_sched,
    _set_context,
    _task,
)


def test_tokens_counted():
    """Reporting reads the same budget that the monitor checks."""
    sched = _make_sched()
    sched.pipeline._checkpointer.budget.record(1234, 0)
    assert sched.pipeline.summary()["tokens_used"] == 1234


@pytest.mark.asyncio
async def test_stop_drained():
    """Scheduler should stop immediately when frontier is empty and buffer empty."""
    sched = _make_sched()

    sched.pipeline._goal = _goal(max_pages=5)
    sched.pipeline._task = _task()
    sched.pipeline._task.state = "RUNNING"
    _set_context(
        sched,
        max_pages=5,
        max_tokens=100000,
        max_duration_sec=3600,
        relevance_threshold=0.7,
    )

    # Mock frontier.pop_next to return None immediately.
    sched.pipeline._frontier.pop_next = AsyncMock(return_value=None)

    reason = await sched._supervise(asyncio.sleep(10))

    assert sched._stopping
    assert reason != "COMPLETED"


@pytest.mark.asyncio
async def test_stop_pages():
    """Scheduler should stop when pages_fetched reaches max_pages."""
    sched = _make_sched()

    sched.pipeline._goal = _goal(max_pages=10)
    sched.pipeline._task = _task()
    sched.pipeline._task.state = "RUNNING"
    _set_context(
        sched,
        max_pages=10,
        pages_fetched=10,  # Already at budget.
        max_tokens=100000,
        max_duration_sec=3600,
        relevance_threshold=0.7,
    )

    reason = await sched._supervise(asyncio.sleep(10))

    # BUDGET_PAGES fires immediately, so it never reaches pop_next.
    assert sched._stopping
    assert reason != "COMPLETED"


@pytest.mark.asyncio
async def test_gate_blocks():
    """Committed budget (fetched + in-flight) must block new pops.

    Regression: the pump used to keep popping while fetches were in
    the air, overshooting max_pages by up to fetch_concurrency-1.
    """
    sched = _make_sched()
    sched.pipeline._goal = _goal(max_pages=10)
    sched.pipeline._task = _task()
    sched.pipeline._task.state = "RUNNING"
    _set_context(
        sched,
        max_pages=10,
        pages_fetched=8,
        in_flight=2,  # 8 + 2 = 10 committed: nothing may be popped
    )

    pop_mock = AsyncMock(return_value=None)
    sched.pipeline._frontier.pop_next = pop_mock

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sched.pipeline._fetch_pump(), timeout=0.5)

    pop_mock.assert_not_called()


@pytest.mark.asyncio
async def test_gate_allows():
    """Below budget, pops still happen (gate is a cap, not a stall)."""
    sched = _make_sched()
    sched.pipeline._goal = _goal(max_pages=10)
    sched.pipeline._task = _task()
    sched.pipeline._task.state = "RUNNING"
    _set_context(
        sched,
        max_pages=10,
        pages_fetched=8,
        in_flight=1,  # 9 committed < 10: one more pop is allowed
    )

    pop_mock = AsyncMock(return_value=None)
    sched.pipeline._frontier.pop_next = pop_mock
    sched.pipeline._frontier.size = 0
    sched.pipeline._frontier.waiting_size = 0

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sched.pipeline._fetch_pump(), timeout=0.5)

    pop_mock.assert_called()


@pytest.mark.asyncio
async def test_aclose():
    """Shutdown must release every stage-held resource.

    Each of these owns something that outlives the run: drain tasks and
    caches in the ranker, a retry queue in the analyzer.  A leaked
    aiosqlite connection keeps its worker thread, and the process hangs
    instead of exiting.
    """
    ranker = MagicMock(aclose=AsyncMock())
    storage = MagicMock(close=AsyncMock())
    analyzer = MagicMock(aclose=AsyncMock())
    sched = _make_sched(ranker=ranker, storage=storage, analyzer=analyzer)
    await sched.aclose()
    ranker.aclose.assert_awaited_once()
    storage.close.assert_awaited_once()
    assert sched.pipeline._page._analysis._drain_task is None


def test_context_needs_key():
    sched = _make_sched()
    sched.pipeline._tracking.record_page_context("", {"title": "T"})
    assert "" not in sched.pipeline.run_state.page_contexts


def test_summary_stats():
    """summary() reads counters and stats straight from the context."""
    sched = _make_sched()
    _set_context(sched, pages_fetched=12, tokens_used=5000, started_at=100.0)
    sched.pipeline.run_state.stats.links_discovered = 123
    sched.pipeline.run_state.stats.candidates_ranked = 45
    sched.pipeline.run_state.stats.fetch_errors = 2
    sched.pipeline.run_state.stats.analyses_by_class = {"RELEVANT": 3, "IRRELEVANT": 1}

    summary = sched.pipeline.summary()

    assert summary["pages_fetched"] == 12
    assert summary["tokens_used"] == 5000
    assert summary["candidates_discovered"] == 123
    assert summary["candidates_ranked"] == 45
    assert summary["fetch_errors"] == 2
    assert summary["analyses"] == {"RELEVANT": 3, "IRRELEVANT": 1}


# end-of-run accounting ---------------------------------------------------


def test_unfinished_log(caplog):
    """Stopping early and finishing look identical from the outside.

    A missing session gave COMPLETED with no pages; a per-domain ceiling
    gave COMPLETED with a hundred and sixty candidates still queued; a
    rank batch landing after the last fetch gave COMPLETED for an account
    that was never opened. None of them said anything.
    """
    sched = _make_sched()
    _set_context(sched, pages_fetched=45)
    sched.pipeline._frontier.size = 16
    sched.pipeline._frontier.waiting_size = 4

    with caplog.at_level(logging.DEBUG):
        sched.pipeline._reconcile()

    assert "task.reconcile" in caplog.text
    assert "task.unfinished" in caplog.text
    assert "20 candidates were never read" in caplog.text


def test_complete_quiet(caplog):
    sched = _make_sched()
    _set_context(sched, pages_fetched=10)
    sched.pipeline._frontier.size = 0
    sched.pipeline._frontier.waiting_size = 0

    with caplog.at_level(logging.DEBUG):
        sched.pipeline._reconcile()

    assert "task.reconcile" in caplog.text
    assert "task.unfinished" not in caplog.text


@pytest.mark.asyncio
async def test_refusal_stops():
    """The engine has to act on the difference, not just record it.

    Before this the harvester's verdict reached a log line and stopped
    there, so a rate-limited crawl kept requesting pages that would all
    be refused, and reported the empty result as a finished run.
    """
    from crawlme.platforms import PageProblem

    sched = _make_sched()
    sched.pipeline.run_state.stats.reset()

    sched.pipeline._tracking.problem(PageProblem.UNAVAILABLE)
    assert sched.pipeline.run_state.progress.refused_by == "", "a gone account is not a reason to stop"

    sched.pipeline._tracking.problem(PageProblem.BLOCKED)
    assert sched.pipeline.run_state.progress.refused_by == "blocked"

    # Later refusals do not overwrite: the first one is what ended it.
    sched.pipeline._tracking.problem(PageProblem.LOGIN_REQUIRED)
    assert sched.pipeline.run_state.progress.refused_by == "blocked"

    assert sched.pipeline.run_state.stats.not_content == {"unavailable": 1, "blocked": 1, "login_required": 1}
    assert sched.pipeline.summary()["not_content"] == {"unavailable": 1, "blocked": 1, "login_required": 1}


# shutdown ordering -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_proposed_seed_that_found_nothing_says_so():
    sched = _make_sched()
    sched.pipeline.run_state.proposed_seeds = {"k": ("https://ig.test/acct/", "why")}
    why, tally = sched.pipeline.summary()["proposed_seeds"]["https://ig.test/acct/"]
    assert (why, tally[0]) == ("why", 0)


def test_failure_reason():
    from crawlme.errors import Error

    sched = _make_sched()
    sched.pipeline._fail("pipeline", Error("provider rejected the request", source="pipeline", code="upstream_failed"))
    assert sched.pipeline.run_state.progress.fatal_error == "provider rejected the request"


def test_the_first_failure_is_the_one_reported():
    sched = _make_sched()
    sched.pipeline._fail("pipeline", RuntimeError("first"))
    sched.pipeline._fail("pipeline", RuntimeError("second"))
    assert sched.pipeline.run_state.progress.fatal_error == "first"


def test_enough_found_reads_the_target():
    sched = _make_sched()
    sched.pipeline.run_state.limits = dataclasses.replace(sched.pipeline.run_state.limits, max_relevant=15)
    sched.pipeline.run_state.progress.relevant_found = 14
    assert sched._allow_finish("analysis") is True
    sched.pipeline.run_state.progress.relevant_found = 15
    assert sched._allow_finish("analysis") is False


def test_no_target_never_enough():
    """Zero means the run was given no target, not a target of zero."""
    sched = _make_sched()
    sched.pipeline.run_state.limits = dataclasses.replace(sched.pipeline.run_state.limits, max_relevant=0)
    sched.pipeline.run_state.progress.relevant_found = 99
    assert sched._allow_finish("analysis") is True
