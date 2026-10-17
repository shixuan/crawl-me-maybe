"""Re-analyze stored pages with bounded concurrency and shared analysis retries."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crawlme.config import Settings
from crawlme.errors import Error, Kind, Skipped
from crawlme.llm import TokenBudget, TokenBudgetError
from crawlme.persist import Storage
from crawlme.persist.read import goal_from_row, page_from_row
from crawlme.pioneer.goal_enhancer import GoalEnhancer
from crawlme.runtime import Goal, Task, spec_version
from crawlme.scheduler.pipelines import Pipeline, PipelineContext
from crawlme.scheduler.pipelines.dedup import DedupPipeline
from crawlme.scheduler.workers import AnalysisWorker

logger = logging.getLogger(__name__)


class ReplayError(Error):
    """Replay cannot proceed: task not found, no credentials, etc."""

    source = "replay"
    code = "replay_failed"
    kind = Kind.INVALID_INPUT


@dataclass(frozen=True)
class ReplayInput:
    task: Task
    run_dir: Path
    prompt: str | None = None
    limit: int | None = None
    force: bool = False


@dataclass
class ReplayResult:
    """The numbers a replay run ends with (rendered by the CLI)."""

    task_id: str
    run_dir: Path
    state: str
    goal_id: str
    new_goal: bool
    pages_total: int
    analyzed: int
    retried_ok: int
    skipped: int
    empty: int
    failed: int
    published: int
    usage: dict[str, Any]
    dedup: dict[str, Any]


class ReplayPipeline(Pipeline[ReplayInput, ReplayResult]):
    def __init__(
        self,
        settings: Settings,
        *,
        storage: Storage,
        analysis: AnalysisWorker,
        enhancer: GoalEnhancer,
        budget: TokenBudget,
        dedup: DedupPipeline,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._analysis = analysis
        self._enhancer = enhancer
        self._budget = budget
        self._dedup = dedup

    async def run(self, input: ReplayInput, context: PipelineContext) -> ReplayResult:
        settings, storage, worker = self._settings, self._storage, self._analysis
        budget, error_handler = self._budget, worker.error_handler
        task_id, run_dir = input.task.task_id, input.run_dir
        prompt, limit, force = input.prompt, input.limit, input.force
        await storage.start()
        goal_row = await storage.get_goal(input.task.goal_id)
        if goal_row is None:
            raise ReplayError(
                f"goal {input.task.goal_id} missing from the run database",
                code="goal_missing",
                kind=Kind.NOT_FOUND,
                details={"goal_id": input.task.goal_id},
            )
        goal = goal_from_row(goal_row)
        new_goal = False
        if prompt:
            # Prompt-derived goal IDs make repeated replay requests idempotent.
            goal = Goal(prompt=prompt)
            existing = await storage.get_goal(goal.goal_id)
            if existing is None:
                new_goal = True
                enhancer = self._enhancer
                enhanced = await error_handler.call(
                    "goal", lambda: enhancer.enhance(goal), optional=True, repeatable=True
                )
                if enhanced is not None and not isinstance(enhanced, Skipped):
                    goal.goal_statement = enhanced.statement
                    goal.keywords = enhanced.keywords
                    goal.since = enhanced.since
                    goal.extraction_spec = enhanced.extraction_spec
                    goal.time_policy = enhanced.time_policy
                storage.save_goal(goal.model_dump(mode="json"))
            else:
                if existing.get("prompt") != prompt:
                    # Reject goal-ID collisions before save_goal can replace a different prompt.
                    raise ReplayError(
                        f"goal id {goal.goal_id} already exists with a different prompt",
                        code="goal_conflict",
                        kind=Kind.CONFLICT,
                        details={"goal_id": goal.goal_id},
                    )
                # Same prompt replayed before: reuse the stored goal
                # (already enhanced), never pay the enhancer again.
                goal = goal_from_row(existing)

        published = 0

        def completed(seed: str | None) -> None:
            nonlocal published
            published += 1

        worker.bind_completion(completed)

        rows = await storage.list_pages()
        pages_total = len(rows)
        if limit is not None:
            rows = rows[:limit]
        analyzed = parked = skipped = empty = 0
        remaining = iter(rows)
        exhausted = False

        async def work() -> None:
            nonlocal analyzed, parked, skipped, empty, exhausted
            for row in remaining:
                if exhausted:
                    return
                page = page_from_row(row)
                # Mirrors the analyzer's own empty-text skip, so the
                # report can tell "no text" apart from "parked".
                if not ((page.plain_text or "").strip() or (page.markdown or "").strip()):
                    empty += 1
                    continue
                if not force and await storage.has_analysis(
                    page.url_key,
                    goal.goal_id,
                    settings.llm_model,
                    spec_version(goal.extraction_spec, goal.time_policy),
                ):
                    skipped += 1
                    continue
                if exhausted:
                    return
                try:
                    budget.check()
                    result = await worker.analyze(page, goal)
                except TokenBudgetError:
                    if not exhausted:
                        logger.warning("replay.budget_exhausted used=%d/%d", budget.used, budget.limit)
                    exhausted = True
                    return
                if isinstance(result, Skipped):
                    if result.reason == "limit_reached":
                        exhausted = True
                        return
                    parked += 1
                elif result is not None:
                    analyzed += 1
                else:
                    parked += 1

        async def analyze_pages() -> None:
            workers = [asyncio.create_task(work()) for _ in range(min(max(1, settings.llm_concurrency), len(rows)))]
            try:
                await error_handler.watch(asyncio.gather(*workers))
            finally:
                for task in workers:
                    task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

            # Parked pages retry in the background; wait them out so every
            # one settles before the analyzer (and this run) closes.
            await worker.drain_pending()

        await context.supervise(analyze_pages())
        retried_ok = published - analyzed
        failed = parked - retried_ok
        await worker.aclose()
        dedup = await self._dedup.run(goal, context)

        return ReplayResult(
            task_id=task_id,
            run_dir=run_dir,
            state=input.task.state,
            goal_id=goal.goal_id,
            new_goal=new_goal,
            pages_total=pages_total,
            analyzed=analyzed,
            retried_ok=retried_ok,
            skipped=skipped,
            empty=empty,
            failed=failed,
            published=published,
            usage=budget.dump(),
            dedup=dedup,
        )

    async def aclose(self) -> None:
        try:
            await self._analysis.aclose()
        finally:
            try:
                await self._dedup.aclose()
            finally:
                await self._storage.close()
