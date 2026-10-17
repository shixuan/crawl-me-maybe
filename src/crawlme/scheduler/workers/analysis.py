"""Apply analyses and checkpoint retries selected by pipeline error policy."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from crawlme.analysis import AnalysisResult, Analyzer
from crawlme.checkpoint import Checkpointable
from crawlme.digest import Page
from crawlme.errors import Action, ErrorContext, ErrorHandler, Skipped
from crawlme.persist import Storage
from crawlme.runtime import Goal, Tracker
from crawlme.util.ids import _content_id


class AnalysisWorker(Checkpointable):
    def __init__(
        self,
        analyzer: Analyzer | None,
        *,
        concurrency: int,
        storage: Storage,
        tracking: Tracker | None = None,
        error_handler: ErrorHandler,
    ) -> None:
        self.analyzer = analyzer
        self.error_handler = error_handler
        self._slots = asyncio.Semaphore(concurrency)
        self._storage = storage
        self._tracking = tracking
        self._goal: Goal | None = None
        self._task_id: str | None = None
        self._completed: Callable[[str | None], None] | None = None
        self._pending: asyncio.Queue[tuple[Page, Goal, int, float]] = asyncio.Queue()
        self._retry_entries: dict[str, tuple[Page, int]] = {}
        self._drain_task: asyncio.Task[None] | None = None

    def prepare(self, goal: Goal, task_id: str) -> None:
        self._task_id, self._goal = task_id, goal

    def dump(self) -> dict[str, Any]:
        return {
            "entries": [
                {"page": page.model_dump(mode="json"), "attempts": attempts}
                for page, attempts in self._retry_entries.values()
            ]
        }

    def restore(self, state: dict[str, Any]) -> None:
        if self._goal is None:
            raise ValueError("prepare analysis before restoring retries")
        if state["entries"] and self.analyzer is None:
            raise ValueError("checkpoint needs an analyzer; configure LLM credentials before resuming")
        for raw in state["entries"]:
            page, attempts = Page.model_validate(raw["page"]), int(raw["attempts"])
            self._retry_entries[page.url_key] = (page, attempts)
            self._pending.put_nowait((page, self._goal, attempts, 0))

    def bind_completion(self, completed: Callable[[str | None], None]) -> None:
        self._completed = completed

    def apply(self, result: AnalysisResult) -> None:
        if self._task_id is not None:
            result.analysis_id = _content_id(f"{self._task_id}:{result.goal_id}:{result.url_key}")
            for index, item in enumerate(result.items or []):
                item.item_id = _content_id(f"{result.analysis_id}:{index}")
        self._storage.save_analysis(result.model_dump(mode="json"))
        seed = self._tracking.analysis(result) if self._tracking is not None else None
        if self._completed is not None:
            self._completed(seed)

    async def analyze(
        self,
        page: Page,
        goal: Goal,
        *,
        allowed: Callable[[], bool] = lambda: True,
    ) -> AnalysisResult | Skipped | None:
        return await self._attempt(page, goal, 1, allowed)

    async def _attempt(
        self,
        page: Page,
        goal: Goal,
        attempt: int,
        allowed: Callable[[], bool],
    ) -> AnalysisResult | Skipped | None:
        if self.analyzer is None:
            return None
        async with self._slots:
            self.error_handler.check()
            if not allowed():
                return None
            try:
                result = await self.analyzer.analyze(page, goal)
            except Exception as exc:
                decision = self.error_handler.decide(exc, ErrorContext("analysis", attempt, True, True))
                if decision.action == Action.RETRY:
                    self._retry_entries[page.url_key] = (page, attempt)
                    self._pending.put_nowait((page, goal, attempt, decision.delay))
                    self.start_retries()
                    return None
                self._retry_entries.pop(page.url_key, None)
                return Skipped(self.error_handler.describe(exc, "analysis"), attempt, decision.reason)
            if result is not None:
                self.apply(result)
            self._retry_entries.pop(page.url_key, None)
            return result

    @property
    def pending_keys(self) -> set[str]:
        return set(self._retry_entries)

    def start_retries(self) -> None:
        if not self._pending.empty() and self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while True:
                page, goal, attempt, delay = await self._pending.get()
                try:
                    await asyncio.sleep(delay)
                    await self._attempt(page, goal, attempt + 1, lambda: True)
                finally:
                    self._pending.task_done()
        except Exception as exc:
            raise self.error_handler.abort(exc, "analysis")

    async def drain_pending(self) -> None:
        await self.error_handler.watch(self._pending.join())

    async def aclose(self) -> None:
        if self._drain_task is not None:
            self._drain_task.cancel()
            await asyncio.gather(self._drain_task, return_exceptions=True)
            self._drain_task = None
        self._retry_entries.clear()
        while not self._pending.empty():
            self._pending.get_nowait()
            self._pending.task_done()
