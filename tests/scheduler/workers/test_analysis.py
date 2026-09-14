from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.analyzer import PageAnalyzer
from crawlme.llm import LLMError, LLMResponse
from crawlme.scheduler.workers import AnalysisWorker
from crawlme.schemas import URL, AnalysisResult, CrawlGoal, Page


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
    worker = AnalysisWorker(analyzer, concurrency=1)
    goal = CrawlGoal(prompt="test")
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
                LLMError("temporary"),
                LLMResponse(
                    content='{"classification":"RELEVANT","relevance_score":0.9}',
                    input_tokens=10,
                    output_tokens=5,
                    model="test",
                ),
            ]
        )
    )
    analyzer = PageAnalyzer(client, retry_delay=0)
    worker = AnalysisWorker(analyzer, concurrency=1)
    results = []
    worker.bind_sink(results.append)
    try:
        assert await worker.analyze(_page(), CrawlGoal(prompt="test"), allowed=lambda: True) is None
        await asyncio.wait_for(analyzer.drain_pending(), timeout=2)
        assert len(results) == 1
        assert results[0].url_key == "p"
        assert results[0].classification == "RELEVANT"
    finally:
        await worker.aclose()
