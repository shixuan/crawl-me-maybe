from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.analysis import AnalysisResult, Analyzer
from crawlme.digest import Page
from crawlme.errors import ErrorHandler, Kind
from crawlme.llm import LLMResponse, RemoteError
from crawlme.runtime import Goal, Limits, Progress, RunState, Stats, Tracker
from crawlme.scheduler.workers import AnalysisWorker
from crawlme.urls import URL


def _page() -> Page:
    return Page(
        url_key="p",
        url=URL(raw="https://example.com/p", canonical="https://example.com/p", url_key="p"),
        plain_text="A page with enough text to analyze.",
    )


@pytest.mark.asyncio
async def test_admission_after_wait():
    started = asyncio.Event()
    release = asyncio.Event()
    allowed = True

    async def analyze(page, goal):
        started.set()
        await release.wait()
        return AnalysisResult(url_key=page.url_key)

    analyzer = MagicMock(analyze=AsyncMock(side_effect=analyze))
    storage = MagicMock()
    worker = AnalysisWorker(
        analyzer,
        concurrency=1,
        storage=storage,
        tracking=Tracker(RunState(limits=Limits(), progress=Progress(), stats=Stats())),
        error_handler=ErrorHandler(),
    )
    goal = Goal(prompt="test")
    first = asyncio.create_task(worker.analyze(_page(), goal, allowed=lambda: allowed))
    await started.wait()
    second = asyncio.create_task(worker.analyze(_page(), goal, allowed=lambda: allowed))
    await asyncio.sleep(0)
    allowed = False
    release.set()
    a, b = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    assert a.url_key == "p"
    assert b is None
    assert analyzer.analyze.await_count == 1


@pytest.mark.asyncio
async def test_retry_reaches_sink():
    client = MagicMock(
        chat=AsyncMock(
            side_effect=[
                RemoteError("temporary", kind=Kind.UNAVAILABLE),
                LLMResponse(
                    content='{"classification":"RELEVANT","relevance_score":0.9}',
                    input_tokens=10,
                    output_tokens=5,
                    model="test",
                ),
            ]
        )
    )
    analyzer = Analyzer(client)
    storage = MagicMock()
    worker = AnalysisWorker(
        analyzer,
        concurrency=1,
        storage=storage,
        tracking=Tracker(RunState(limits=Limits(), progress=Progress(), stats=Stats())),
        error_handler=ErrorHandler(),
    )
    results = []
    worker.bind_completion(lambda url_key, seed: results.append((url_key, seed)))
    try:
        assert await worker.analyze(_page(), Goal(prompt="test"), allowed=lambda: True) is None
        await asyncio.wait_for(worker.drain_pending(), timeout=3)
        assert len(results) == 1
        assert results[0][0] == "p"
        saved = storage.save_analysis.call_args.args[0]
        assert saved["url_key"] == "p"
        assert saved["classification"] == "RELEVANT"
        assert worker._tracking.state.stats.analyses_by_class == {"RELEVANT": 1}
    finally:
        await worker.aclose()
