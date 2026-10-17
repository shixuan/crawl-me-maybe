"""Execute standalone and composed pipelines without crawl models or storage."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from crawlme.scheduler.engine import Engine, create_engine
from crawlme.scheduler.monitor import Monitor
from crawlme.scheduler.pipelines import Pipeline, PipelineContext
from crawlme.scheduler.stop_conds import StopReason


class ManualMonitor(Monitor):
    def __init__(self):
        self.stopped = asyncio.Event()
        self.hard = asyncio.Event()
        self.closed = False

    def allowed(self, phase):
        return not self.hard.is_set() and (phase != "run" or not self.stopped.is_set())

    def reasons(self):
        return [StopReason("REQUESTED")] if self.stopped.is_set() else []

    async def wait(self, *, phase="run", deadline=None):
        event = self.stopped if phase == "run" else self.hard
        timeout = None if deadline is None else max(0, deadline - time.monotonic())
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        return self.reasons()

    def close(self):
        self.closed = True


class Add(Pipeline[int, int]):
    def __init__(self, amount):
        self.amount = amount
        self.closed = False

    async def run(self, input, context):
        return input + self.amount

    async def aclose(self):
        self.closed = True


class Sum(Pipeline[int, int]):
    def __init__(self, *children):
        self.children = children

    async def run(self, input, context):
        for child in self.children:
            input = await child.run(input, context)
        return input

    async def aclose(self):
        for child in self.children:
            await child.aclose()


async def test_pipeline_composition():
    engine = Engine()
    one = Add(1)
    engine.bind(one)
    assert await engine.run(10) == 11
    assert one.closed
    first, last = Add(2), Add(3)
    assert await create_engine(Sum(first, Sum(last))).run(10) == 15
    assert first.closed and last.closed


class Records(Pipeline[int, list[int]]):
    def __init__(self, mode):
        self.mode = mode
        self.entered = asyncio.Event()
        self.settling = asyncio.Event()
        self.records = []
        self.closed = False
        self.reason = None

    async def run(self, input, context: PipelineContext):
        self.context = context
        await context.checkpoint()
        try:
            self.reason = await context.supervise(self.work(input))
            await context.settle(self.write, "write")
        finally:
            await context.stop_checkpoints()
        return self.records

    async def work(self, value):
        self.entered.set()
        if self.mode == "fail":
            raise RuntimeError("work failed")
        if self.mode != "complete":
            await asyncio.Event().wait()
        self.records.append(value)

    async def write(self):
        self.settling.set()
        if self.mode == "settle_fail":
            raise RuntimeError("write failed")
        if self.mode in {"deadline", "hard_stop", "checkpoint_fail", "cancel_settle"}:
            await asyncio.Event().wait()
        self.records.append(99)

    async def aclose(self):
        self.closed = True
        await self.context.checkpoint()


async def test_generic_completion():
    pipeline = Records("complete")
    save = AsyncMock()
    engine = create_engine(pipeline, checkpoint=save)
    assert await engine.run(5) == [5, 99]
    assert pipeline.reason == "COMPLETED"
    assert pipeline.closed
    save.assert_awaited_once()
    assert not engine._checkpoint_saves and engine._checkpoint_task.done()


@pytest.mark.parametrize(
    "mode", ["stop", "cancel", "fail", "settle_fail", "deadline", "hard_stop", "checkpoint_fail", "cancel_settle"]
)
async def test_generic_stop(monkeypatch, mode):
    pipeline = Records(mode)
    monitor = ManualMonitor()
    save = AsyncMock()
    engine = create_engine(pipeline, checkpoint=save, monitor=monitor)
    monkeypatch.setattr("crawlme.scheduler.engine.engine._SETTLE_TIMEOUT", 0.05 if mode == "deadline" else 5)
    if mode == "checkpoint_fail":
        monkeypatch.setattr("crawlme.scheduler.engine.engine._CHECKPOINT_INTERVAL", 0.01)
        save.side_effect = [None, RuntimeError("save failed")]
    running = asyncio.create_task(engine.run(5))
    try:
        await asyncio.wait_for(pipeline.entered.wait(), 2)
        if mode in {"cancel", "cancel_settle"}:
            if mode == "cancel_settle":
                monitor.stopped.set()
                await asyncio.wait_for(pipeline.settling.wait(), 2)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(running, 2)
        else:
            monitor.stopped.set()
            if mode == "hard_stop":
                await asyncio.wait_for(pipeline.settling.wait(), 2)
                monitor.hard.set()
            if mode in {"fail", "settle_fail", "checkpoint_fail"}:
                with pytest.raises(RuntimeError, match="failed"):
                    await asyncio.wait_for(running, 2)
            else:
                assert await asyncio.wait_for(running, 2) == ([99] if mode == "stop" else [])
        assert monitor.closed and pipeline.closed
        assert not engine._checkpoint_saves and engine._checkpoint_task.done()
        assert save.await_count == (2 if mode == "checkpoint_fail" else 1)
        assert not engine.context.can_start("run")
        assert not engine.context.can_finish("write")
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_rejects_concurrent_pipeline():
    engine = Engine()
    first = Records("wait")
    second = Add(1)
    engine.bind(first)
    running = asyncio.create_task(engine.run(5))
    try:
        await asyncio.wait_for(first.entered.wait(), 2)
        with pytest.raises(RuntimeError, match="already bound"):
            engine.bind(second)
        with pytest.raises(RuntimeError, match="already started"):
            await engine.run(10)
        assert not second.closed
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert first.closed


async def test_binding_guards():
    engine = Engine()
    with pytest.raises(RuntimeError, match="no pipeline"):
        await engine.run(1)
    pipeline = Add(1)
    engine.bind(pipeline)
    with pytest.raises(RuntimeError, match="already bound"):
        engine.bind(Add(2))
    await engine.aclose()
    assert pipeline.closed
    with pytest.raises(RuntimeError, match="closed"):
        await engine.run(1)
