"""Exercise the shared execution contract without an engine supervising it."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.runtime import Task
from crawlme.scheduler.pipelines import Pipeline
from crawlme.scheduler.pipelines.crawl import CrawlInput
from tests.scheduler.test_control import build, candidate


async def test_cancel_composed_pipeline(tmp_path):
    entered = asyncio.Event()

    async def fetch(item):
        entered.set()
        await asyncio.Event().wait()

    scheduler, goal, _, _ = build(tmp_path, fetcher=MagicMock(fetch=fetch, aclose=AsyncMock()))
    scheduler.pipeline.run_state.reset(goal=goal)
    await scheduler.pipeline._storage.start()
    await scheduler.pipeline.ingest_seeds(goal, [candidate()])
    pipeline: Pipeline[CrawlInput, Task] = scheduler.pipeline
    running = asyncio.create_task(pipeline.run(CrawlInput(goal, Task(goal_id=goal.goal_id)), scheduler.engine.context))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 2)
        await pipeline.aclose()
        assert not scheduler.pipeline._pump_tasks
        assert not scheduler.pipeline._inflight
        assert scheduler.pipeline.run_state.progress.in_flight == 0
        assert scheduler.pipeline.summary()["dedup"]["status"] == "disabled"
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await scheduler.aclose()
