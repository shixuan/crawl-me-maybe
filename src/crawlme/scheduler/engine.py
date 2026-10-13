"""Own run lifecycle, stopping policy and checkpoint timing."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Any, Literal

from crawlme.checkpoint import Checkpointer
from crawlme.config import Settings
from crawlme.logging import setup_logging
from crawlme.persist import Lock, Storage
from crawlme.runtime import EventEmitter, EventType, RunState
from crawlme.scheduler.monitor import StopMonitor
from crawlme.scheduler.pipelines import PipelineContext
from crawlme.scheduler.pipelines.crawl import CrawlPipeline
from crawlme.scheduler.reporting import summary
from crawlme.scheduler.stop_conds import StopReason
from crawlme.schemas import CrawlGoal, CrawlTask

logger = logging.getLogger(__name__)
_CHECKPOINT_INTERVAL = 2.0
_SETTLE_TIMEOUT = 120.0


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Engine:
    """Apply run policy to a pipeline without invoking individual crawl stages."""

    def __init__(
        self,
        *,
        settings: Settings,
        storage: Storage,
        pipeline: CrawlPipeline,
        checkpointer: Checkpointer,
        monitor: StopMonitor,
    ) -> None:
        self.pipeline = pipeline
        self._cfg = settings
        self._storage = storage
        self._checkpointer = checkpointer
        self._task: CrawlTask | None = None
        self._events: EventEmitter | None = None
        self._runner: asyncio.Task[Any] | None = None
        self._pipeline_task: asyncio.Task[None] | None = None
        self._monitor = monitor
        self._closed = False
        self._checkpoint_task: asyncio.Task[None] | None = None

    def attach_log_file(self) -> None:
        """Start writing logs to the run dir's log file.

        Called by the CLI before the Goal Enhancer runs, so those early
        logs land in the file, not just the terminal.
        """
        self._storage.attach_log_file()

    async def run(
        self,
        goal: CrawlGoal,
        task: CrawlTask,
        *,
        checkpoint: dict[str, Any] | None = None,
        lock: Lock | None = None,
    ) -> None:
        if self._runner is not None or self._closed:
            raise RuntimeError("scheduler already started or closed")
        own_lock = lock is None
        lock = lock or Lock(self._storage.db_path)
        if own_lock:
            lock.acquire()
        self._runner = asyncio.current_task()
        self._task = task
        task.state = "RUNNING"
        setup_logging(self._cfg)
        logger.info(
            "crawling, at most %d pages or %d tokens or %ds", goal.max_pages, goal.max_tokens, goal.max_duration_sec
        )
        try:
            await self._storage.start()
            self._events = EventEmitter(self._storage, task.task_id)
            self._events.emit(
                EventType.TASK_RESUMED if checkpoint is not None else EventType.TASK_STARTED,
                {"goal_id": goal.goal_id, "prompt": goal.prompt[:200]},
            )
            self.run_state.reset(goal=goal)
            self.pipeline.prepare(goal, task.task_id, self._events)
            self._checkpointer.bind(goal, task, **self.pipeline.features)
            if checkpoint is not None:
                await self._checkpointer.restore(checkpoint)
            self._storage.save_goal(goal.model_dump(mode="json"))
            self._storage.save_task(task.model_dump(mode="json"))
            await self._checkpoint()
            self._checkpoint_task = asyncio.create_task(self._checkpoint_loop())
            context = PipelineContext(
                task_id=task.task_id,
                events=self._events,
                checkpoint=self._checkpoint,
                can_dispatch=self._allow_dispatch,
                can_run=self._allow_work,
                can_analyze=self._allow_analysis,
            )
            self._pipeline_task = asyncio.create_task(self.pipeline.run(goal, context))
            # Let restored work register before checking whether the pipeline is drained.
            await asyncio.sleep(0)
            await self._supervise()
            deadline = time.monotonic() + _SETTLE_TIMEOUT
            await self.pipeline.halt()
            await self._settle(deadline)
            await self._drain(deadline)
            if not self.run_state.progress.fatal_error:
                await self.pipeline.finish()
            self._finish_run()
        except asyncio.CancelledError:
            task.state = "INTERRUPTED"
            await self.pipeline.abort()
            task.stopping_reason = "USER_REQUESTED"
            task.counters = {
                "pages_fetched": self.run_state.progress.pages_fetched,
                "tokens_used": self._monitor.budget.used,
            }
            self._storage.save_task(task.model_dump(mode="json"))
            logger.info("interrupted; resume with: crawl resume %s", task.task_id)
            raise
        except Exception as exc:
            self._fail("run", exc)
            await self.pipeline.abort()
            self._finish_run()
        finally:
            if self._pipeline_task is not None:
                self._pipeline_task.cancel()
                await asyncio.gather(self._pipeline_task, return_exceptions=True)
            if self._checkpoint_task is not None:
                self._checkpoint_task.cancel()
                await asyncio.gather(self._checkpoint_task, return_exceptions=True)
            try:
                await self.aclose()
            finally:
                self._runner = None
                if own_lock:
                    lock.release()

    def _finish_run(self) -> None:
        assert self._task is not None
        task = self._task
        task.state = "FAILED" if self.run_state.progress.fatal_error else "COMPLETED"
        task.end_at = _utcnow()
        self._record_stop_reason()
        self._reconcile()
        prog = self.run_state.progress
        logger.info(
            "finished after %d pages and %d tokens: %s",
            prog.pages_fetched,
            self._monitor.budget.used,
            task.stopping_reason,
        )
        if self._events:
            self._events.emit(EventType.STOPPED, {"reason": task.stopping_reason, "pages_fetched": prog.pages_fetched})
        task.counters = {"tokens_used": self._monitor.budget.used, "pages_fetched": prog.pages_fetched}
        self._storage.save_task(task.model_dump(mode="json"))

    def _fail(self, stage: str, error: BaseException) -> None:
        logger.error("%s.died error=%s", stage, error)
        self.run_state.record_failure(error)
        if self._task is not None:
            self._task.stopping_reason = "FATAL"
            self._task.state = "STOPPING"

    def _allow_work(self) -> bool:
        if self._task is None or self._task.state != "RUNNING":
            return False
        return self._monitor.can_run

    def _allow_dispatch(self) -> bool:
        if not self._allow_work():
            return False
        limits, progress = self.run_state.limits, self.run_state.progress
        # Reserve budget for pages already dispatched; failed fetches release their slots.
        return limits.max_pages <= 0 or progress.pages_fetched + progress.in_flight < limits.max_pages

    def _allow_analysis(self) -> bool:
        if self._task is None or self._task.state == "INTERRUPTED":
            return False
        return self._monitor.can_analyze

    async def _wait_for_stop(
        self,
        task: asyncio.Task[None],
        *,
        phase: Literal["run", "pages", "analysis"] = "run",
        deadline: float | None = None,
    ) -> list[StopReason]:
        stopped = asyncio.create_task(self._monitor.wait(phase=phase, deadline=deadline))
        try:
            await asyncio.wait([task, stopped], return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                await task
                return []
            return await stopped
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)

    async def _supervise(self) -> None:
        assert self._pipeline_task is not None and self._task is not None
        reasons = await self._wait_for_stop(self._pipeline_task)
        if reasons:
            self._task.stopping_reason = "+".join(r.code for r in reasons)
            logger.info(
                "stopping: %s, after %d pages", self._task.stopping_reason, self.run_state.progress.pages_fetched
            )
        self._task.state = "STOPPING"

    async def _settle(self, deadline: float) -> None:
        settle = asyncio.create_task(self.pipeline.settle(deadline))
        try:
            await self._wait_for_stop(settle, phase="pages", deadline=deadline)
            if not settle.done():
                if time.monotonic() >= deadline:
                    logger.warning("task.settle_timeout")
                await self.pipeline.abort()
        finally:
            settle.cancel()
            await asyncio.gather(settle, return_exceptions=True)

    async def _drain(self, deadline: float | None = None) -> None:
        """Wait for retry completion or a condition that forbids further analysis."""
        deadline = deadline if deadline is not None else time.monotonic() + _SETTLE_TIMEOUT
        drain: asyncio.Task[None] | None = None
        try:
            if self._monitor.can_analyze:
                drain = asyncio.create_task(self.pipeline.drain())
                await self._wait_for_stop(drain, phase="analysis", deadline=deadline)
                if not drain.done() and time.monotonic() >= deadline:
                    logger.warning("analysis.settle_timeout")
        finally:
            if drain is not None:
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
            await self.pipeline.end_drain()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._monitor.close()
        try:
            await self.pipeline.aclose()
        except Exception as exc:
            self._fail("cleanup", exc)
        if self._task is not None and self.run_state.progress.fatal_error:
            self._task.state = "FAILED"
            self._storage.save_task(self._task.model_dump(mode="json"))
        await self._storage.close()

    def summary(self) -> dict[str, Any]:
        return {**summary(self.run_state, tokens_used=self._monitor.budget.used), **self.pipeline.summary()}

    @property
    def run_state(self) -> RunState:
        """Run data shared with the tracker and read by CLI reporting."""
        return self.pipeline.run_state

    def _reconcile(self) -> None:
        """Report fetched, pending and refused work alongside the stop reason."""
        left_frontier = self._monitor.frontier.size
        left_buffer = self._monitor.frontier.waiting_size
        stats = self.run_state.stats
        logger.debug(
            "task.reconcile discovered=%d ranked=%d fetched=%d left_in_frontier=%d left_in_buffer=%d",
            stats.links_discovered,
            stats.candidates_ranked,
            self.run_state.progress.pages_fetched,
            left_frontier,
            left_buffer,
        )
        if left_frontier or left_buffer:
            logger.warning(
                "task.unfinished %d candidates were never read (frontier=%d buffer=%d)",
                left_frontier + left_buffer,
                left_frontier,
                left_buffer,
            )

    def _record_stop_reason(self) -> None:
        """Name why the run is ending, for a path that bypassed the check."""
        if self._task is None or self._task.stopping_reason:
            return
        reasons = self._monitor.reasons()
        self._task.stopping_reason = "+".join(r.code for r in reasons) if reasons else "FRONTIER_DRAINED"
        logger.debug(
            "stop.on_exit reason=%s pages=%d", self._task.stopping_reason, self.run_state.progress.pages_fetched
        )

    async def _checkpoint(self) -> None:
        if self._task is None:
            return
        if self._task.state == "INTERRUPTED":
            return
        await self._checkpointer.save()
        if self._events:
            self._events.emit(EventType.CHECKPOINT_SAVED, {"pages": self.run_state.progress.pages_fetched})

    async def _checkpoint_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_CHECKPOINT_INTERVAL)
                await self._checkpoint()
        except Exception as exc:
            self._fail("checkpoint", exc)
