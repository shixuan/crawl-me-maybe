"""Run page analysis within its own concurrency and admission limits."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from crawlme.analysis import Analyzer
from crawlme.schemas import AnalysisResult, CrawlGoal, Page


class AnalysisWorker:
    def __init__(self, analyzer: Analyzer | None, *, concurrency: int) -> None:
        self.analyzer = analyzer
        self._slots = asyncio.Semaphore(concurrency)

    def bind_sink(self, sink: Callable[[AnalysisResult], None]) -> None:
        if self.analyzer is not None:
            self.analyzer.bind_sink(sink)

    async def analyze(self, page: Page, goal: CrawlGoal, *, allowed: Callable[[], bool]) -> AnalysisResult | None:
        if self.analyzer is None:
            return None
        async with self._slots:
            # Results received while this page waited may have met the target.
            if allowed():
                return await self.analyzer.analyze(page, goal)
        return None

    async def aclose(self) -> None:
        if self.analyzer is not None:
            await self.analyzer.aclose()
