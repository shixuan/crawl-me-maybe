"""Wait for automatic stop conditions without waking the engine on ordinary progress."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import TYPE_CHECKING, Protocol

from crawlme.llm import TokenBudget
from crawlme.pioneer import Frontier
from crawlme.runtime import RunState
from crawlme.scheduler.stop_conds import StopReason, can_drain_analysis, can_settle_pages, check_stop
from crawlme.util import subscribe

if TYPE_CHECKING:
    from crawlme.scheduler.pipelines.crawl import CrawlPipeline


def create_monitor(pipeline: CrawlPipeline) -> Monitor:
    return StopMonitor(pipeline.run_state, pipeline.frontier, pipeline.budget)


class Monitor(Protocol):
    """Stopping decisions consumed by the engine, independent of work topology."""

    def allowed(self, phase: str) -> bool: ...

    def reasons(self) -> list[StopReason]: ...

    async def wait(self, *, phase: str = "run", deadline: float | None = None) -> list[StopReason]: ...

    def close(self) -> None: ...


class StopMonitor(Monitor):
    def __init__(self, runtime: RunState, frontier: Frontier, budget: TokenBudget) -> None:
        self.runtime = runtime
        self.frontier = frontier
        self.budget = budget
        self._revision = 0
        self._updated = asyncio.Event()
        self._unsubscribe = [
            subscribe(runtime, lambda state: (state.limits, dataclasses.astuple(state.progress)), self._notify),
            subscribe(
                frontier,
                lambda queue: (queue.size, queue.waiting_size, queue.scoring, queue.blocked_by_domain_budget),
                self._notify,
            ),
            subscribe(budget, lambda tokens: tokens.used, self._notify),
        ]

    def close(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()

    def _notify(self) -> None:
        self._revision += 1
        self._updated.set()

    async def _wait_for_update(self, revision: int) -> None:
        while revision == self._revision:
            self._updated.clear()
            await self._updated.wait()

    def reasons(self) -> list[StopReason]:
        return check_stop(
            self.runtime.progress,
            self.runtime.limits,
            tokens_used=self.budget.used,
            queued=self.frontier.size,
            waiting=self.frontier.waiting_size,
            ranking=self.frontier.scoring,
            domain_refused=self.frontier.blocked_by_domain_budget,
        )

    def allowed(self, phase: str) -> bool:
        reasons = self.reasons()
        return self._allowed(phase, reasons)

    def _allowed(self, phase: str, reasons: list[StopReason]) -> bool:
        if phase in {"pages", "settle"}:
            return can_settle_pages(reasons)
        if phase == "analysis":
            return can_drain_analysis(reasons)
        if phase == "dispatch":
            limits, progress = self.runtime.limits, self.runtime.progress
            # Reserve slots for pages already dispatched; failed fetches release them.
            return not reasons and (
                limits.max_pages <= 0 or progress.pages_fetched + progress.in_flight < limits.max_pages
            )
        if phase == "run":
            return not reasons
        raise ValueError(f"unknown crawl phase: {phase}")

    async def wait(self, *, phase: str = "run", deadline: float | None = None) -> list[StopReason]:
        while True:
            revision = self._revision
            reasons = self.reasons()
            if not self._allowed(phase, reasons):
                return reasons
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return []
            limits, progress = self.runtime.limits, self.runtime.progress
            run_deadline = (
                progress.started_at + limits.max_duration_sec
                if limits.max_duration_sec > 0 and progress.started_at > 0
                else None
            )
            deadlines = [d for d in (deadline, run_deadline) if d is not None]
            timeout = max(0, min(deadlines) - now) if deadlines else None
            try:
                await asyncio.wait_for(self._wait_for_update(revision), timeout=timeout)
            except asyncio.TimeoutError:
                pass
