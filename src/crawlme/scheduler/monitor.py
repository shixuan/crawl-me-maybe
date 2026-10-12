"""Wait for automatic stop conditions without waking the engine on ordinary progress."""

from __future__ import annotations

import asyncio
import time
from typing import Literal

from crawlme.llm import TokenBudget
from crawlme.pioneer import Frontier
from crawlme.runtime import RunState
from crawlme.scheduler.stop_conds import StopReason, can_drain_analysis, can_settle_pages, check_stop


class StopMonitor:
    def __init__(self, runtime: RunState, frontier: Frontier, budget: TokenBudget) -> None:
        self.runtime = runtime
        self.frontier = frontier
        self.budget = budget

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

    @property
    def can_run(self) -> bool:
        return not self.reasons()

    @property
    def can_analyze(self) -> bool:
        return can_drain_analysis(self.reasons())

    async def wait(
        self, *, phase: Literal["run", "pages", "analysis"] = "run", deadline: float | None = None
    ) -> list[StopReason]:
        while True:
            revision = self.runtime.revision
            reasons = self.reasons()
            if phase == "pages":
                allowed = can_settle_pages(reasons)
            elif phase == "analysis":
                allowed = can_drain_analysis(reasons)
            else:
                allowed = not reasons
            if not allowed:
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
                await asyncio.wait_for(self.runtime.wait_for_change(revision), timeout=timeout)
            except asyncio.TimeoutError:
                pass
