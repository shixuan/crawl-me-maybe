"""Unit tests for Engine (mock all I/O, verify control flow)."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from tests.scheduler.helpers import (
    _goal,
    _make_sched,
    _set_context,
    _task,
)


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
