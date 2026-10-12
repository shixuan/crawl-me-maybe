"""Run analysis and apply initial and retried results."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from crawlme.analysis import Analyzer
from crawlme.persist import Storage
from crawlme.runtime import Tracker
from crawlme.schemas import AnalysisResult, CrawlGoal, Page
from crawlme.schemas.core import _content_id


class AnalysisWorker:
    def __init__(
        self,
        analyzer: Analyzer | None,
        *,
        concurrency: int,
        storage: Storage,
        tracking: Tracker,
    ) -> None:
        self.analyzer = analyzer
        self._slots = asyncio.Semaphore(concurrency)
        self._storage = storage
        self._tracking = tracking
        self._task_id: str | None = None
        self._completed: Callable[[str | None], None] | None = None
        if analyzer is not None:
            analyzer.bind_sink(self.apply)

    def prepare(self, goal: CrawlGoal, task_id: str) -> None:
        self._task_id = task_id
        if self.analyzer is not None:
            self.analyzer.bind_goal(goal)

    def bind_completion(self, completed: Callable[[str | None], None]) -> None:
        self._completed = completed

    def apply(self, result: AnalysisResult) -> None:
        if self._task_id is not None:
            result.analysis_id = _content_id(f"{self._task_id}:{result.goal_id}:{result.url_key}")
            for index, item in enumerate(result.items or []):
                item.item_id = _content_id(f"{result.analysis_id}:{index}")
        self._storage.save_analysis(result.model_dump(mode="json"))
        seed = self._tracking.analysis(result)
        for work in self._tracking.state.work.values():
            if work.get("page", {}).get("url_key") == result.url_key:
                work["analysis_done"] = True
        if self._completed is not None:
            self._completed(seed)

    async def analyze(self, page: Page, goal: CrawlGoal, *, allowed: Callable[[], bool]) -> AnalysisResult | None:
        if self.analyzer is None:
            return None
        async with self._slots:
            # Results received while this page waited may have met the target.
            if allowed():
                return await self.analyzer.analyze(page, goal)
        return None

    @property
    def pending_keys(self) -> set[str]:
        return self.analyzer.pending_keys if self.analyzer is not None else set()

    def start_retries(self) -> None:
        if self.analyzer is not None:
            self.analyzer.start_retries()

    async def aclose(self) -> None:
        if self.analyzer is not None:
            await self.analyzer.aclose()

    async def drain_pending(self) -> None:
        if self.analyzer is not None:
            await self.analyzer.drain_pending()
