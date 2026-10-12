"""Verify crawl task ownership, seed admission and pump behavior."""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crawlme.config import Settings
from crawlme.schemas import (
    URL,
    Candidate,
)
from tests.scheduler.helpers import (
    _goal,
    _item,
    _make_sched,
    _set_context,
    _task,
)


@pytest.mark.asyncio
async def test_rank_pump_exits():
    """Halting the pipeline cancels ranking blocked on a frontier wakeup."""
    sched = _make_sched()

    async def blocked(_):
        await asyncio.sleep(10)

    sched.pipeline._frontier.wait_for_ranking = blocked
    task = asyncio.create_task(sched.pipeline._rank_pump())
    sched.pipeline._pump_tasks.append(task)
    await asyncio.sleep(0)
    await sched.pipeline.halt()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_pump_quiet(caplog):
    """The rank pump is inside a rank call: it cannot act on a wake.

    Waking it every tick produced a line of log per tick for the whole
    length of the call, saying the buffer had items when it was empty.
    """
    sched = _make_sched()
    sched.pipeline._goal = _goal()
    sched._task = _task()
    sched._task.state = "RUNNING"
    _set_context(
        sched,
    )

    sched.pipeline._frontier.scoring = 11
    sched.pipeline._frontier.pop_next = AsyncMock(return_value=None)
    sched.pipeline._frontier.size = 0
    sched.pipeline._frontier.waiting_size = 0
    wake = AsyncMock()
    sched.pipeline._frontier.wake_ranker = wake

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sched.pipeline._fetch_pump(), timeout=0.3)

    wake.assert_not_called()
    assert "waking_rank" not in caplog.text


def test_rank_drain_once():
    """Nothing in a drained batch is fetchable until all of it is scored.

    At 100 the ranker split the batch into nine calls of its own; the
    first was scored in thirty seconds and reached the frontier four and
    a half minutes later, after the run had stopped for lack of anything
    to fetch. The drain size is that latency, not a throughput knob.
    """
    from crawlme.pioneer.ranker import _BATCH_SIZE
    from crawlme.scheduler.pipelines.crawl import _RANK_BATCH_SIZE

    assert _RANK_BATCH_SIZE <= _BATCH_SIZE, "a drain larger than one call reintroduces the wait"


@pytest.mark.asyncio
async def test_cooldown_lives(caplog):
    """Nothing poppable right now is not the same as nothing left.

    A clock that stepped backwards on the host left the only seed with a
    cooldown in the future. The pop was refused, the pump read that as
    an exhausted frontier, and the run reported itself finished having
    fetched nothing at all.
    """
    sched = _make_sched()
    sched.pipeline._goal = _goal()
    sched._task = _task()
    sched._task.state = "RUNNING"
    _set_context(
        sched,
    )

    sched.pipeline._frontier.pop_next = AsyncMock(return_value=None)
    sched.pipeline._frontier.size = 1
    sched.pipeline._frontier.cooling = 1
    sched.pipeline._frontier.waiting_size = 0

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sched.pipeline._fetch_pump(), timeout=0.3)

    assert "fetch_pump.exhausted" not in caplog.text
    assert sched._task.stopping_reason is None


@pytest.mark.asyncio
async def test_inflight_waits():
    """Wait for dispatched fetches and analyses before closing their resources."""
    sched = _make_sched()
    order: list[str] = []
    released = asyncio.Event()

    async def _slow_fetch():
        await released.wait()
        order.append("fetch")

    sched._storage.close = AsyncMock(side_effect=lambda: order.append("close"))
    task = asyncio.create_task(_slow_fetch())
    sched.pipeline._inflight.add(task)
    task.add_done_callback(sched.pipeline._inflight.discard)

    settling = asyncio.create_task(sched.pipeline.settle(time.monotonic() + 0.05))
    await asyncio.sleep(0)
    assert not settling.done(), "it must wait, not walk past"
    released.set()
    await settling
    await sched.aclose()
    assert order == ["fetch", "close"]


@pytest.mark.asyncio
async def test_inflight_gone(caplog, monkeypatch):
    """A backstop, not a promise: the process must still be able to exit."""
    sched = _make_sched()
    stuck = asyncio.create_task(asyncio.sleep(3600))
    sched.pipeline._inflight.add(stuck)

    with caplog.at_level(logging.WARNING):
        await sched.pipeline.settle(time.monotonic() + 0.05)

    assert stuck.cancelled() or stuck.done()
    assert "settle_timeout" in caplog.text


@pytest.mark.asyncio
async def test_seed_expansion_disabled():
    """Disabled seed expansion does not propose sources."""
    sched = _make_sched(settings=Settings(expand_seeds=False))
    assert await sched.pipeline.expand_seeds(_goal(), [MagicMock()]) == []


@pytest.mark.asyncio
async def test_no_seeds_to_expand():
    """Nothing to widen, and the model would have no example to follow."""
    sched = _make_sched(settings=Settings(expand_seeds=True))
    assert await sched.pipeline.expand_seeds(_goal(), []) == []


@pytest.mark.asyncio
async def test_expanded_seeds_are_marked():
    """The buffer reads this to give them the smaller share."""

    url = URL(raw="https://a.com/", canonical="https://a.com/", url_key="a", reg_domain="a.com")
    proposed = Candidate(url=url, seed_ext=True)
    sched = _make_sched(settings=Settings(expand_seeds=True))
    with patch("crawlme.scheduler.pipelines.crawl.expand", AsyncMock(return_value=([proposed], 1, []))):
        got = await sched.pipeline.expand_seeds(_goal(), [MagicMock(url=url)])
    assert [c.seed_ext for c in got] == [True]


@pytest.mark.asyncio
async def test_backpressure_caps_inflight():
    """The fetch slot is released before the analysis, so tasks waiting
    for an LLM slot hold nothing back. One run reached forty-six parked
    on two analysis slots and abandoned thirty-three when it stopped."""
    sched = _make_sched(settings=Settings(fetch_concurrency=6, llm_concurrency=2))
    sched._task = _task()
    sched._task.state = "RUNNING"
    sched.run_state.progress.in_flight = 6 + 2 * 2
    sched.pipeline._frontier.pop_next = AsyncMock(return_value=_item())

    task = asyncio.create_task(sched.pipeline._fetch_pump())
    await asyncio.sleep(0.05)
    sched._task.state = "STOPPING"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    sched.pipeline._frontier.pop_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_cancelled_pump_is_not_a_failure():
    sched = _make_sched()
    task = asyncio.create_task(asyncio.sleep(10))
    sched.pipeline._pump_tasks.append(task)
    await sched.pipeline.halt()
    assert not sched.run_state.progress.fatal_error
