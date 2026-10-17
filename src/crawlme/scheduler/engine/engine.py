"""Drive pipelines, supervise stopping and control checkpoint timing."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Generic, TypeVar

from crawlme.scheduler.monitor import Monitor
from crawlme.scheduler.pipelines import Pipeline, PipelineContext

logger = logging.getLogger(__name__)
_CHECKPOINT_INTERVAL = 2.0
_SETTLE_TIMEOUT = 120.0

Input = TypeVar("Input")
Output = TypeVar("Output")


class Engine(Generic[Input, Output]):
    def __init__(
        self,
        *,
        monitor: Monitor | None = None,
        checkpoint: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._pipeline: Pipeline[Input, Output] | None = None
        self._monitor = monitor
        self._save = checkpoint
        self._runner: asyncio.Task[Any] | None = None
        self._closed = False
        self._stopping = False
        self._cancelled = False
        self._deadline: float | None = None
        self._checkpoint_task: asyncio.Task[None] | None = None
        self._checkpointing = True
        self._checkpoint_saves: set[asyncio.Task[None]] = set()
        self.context = PipelineContext(
            checkpoint=self._checkpoint,
            stop_checkpoints=self._stop_checkpoints,
            supervise=self._supervise,
            can_start=self._allow_work,
            can_finish=self._allow_finish,
            settle=self._settle_step,
        )

    def bind(self, pipeline: Pipeline[Input, Output]) -> None:
        if self._pipeline is not None or self._runner is not None or self._closed:
            raise RuntimeError("engine already bound, started or closed")
        self._pipeline = pipeline

    async def run(self, input: Input) -> Output:
        if self._runner is not None or self._closed:
            raise RuntimeError("engine already started or closed")
        pipeline = self._pipeline
        if pipeline is None:
            raise RuntimeError("engine has no pipeline")
        self._runner = asyncio.current_task()
        work = asyncio.create_task(pipeline.run(input, self.context))
        try:
            # Cancellation must stop checkpoint writers before pipeline cleanup runs.
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            self._cancelled = True
            raise
        finally:
            await self._stop_checkpoints()
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            try:
                await self.aclose()
            finally:
                self._runner = None

    def _allow_work(self, phase: str = "run") -> bool:
        return not self._stopping and self._allow_finish(phase)

    def _allow_finish(self, phase: str) -> bool:
        return not self._closed and not self._cancelled and (self._monitor is None or self._monitor.allowed(phase))

    async def _wait_for_stop(
        self, task: asyncio.Task[None], *, phase: str = "run", deadline: float | None = None
    ) -> str:
        stopped = asyncio.create_task(self._monitor.wait(phase=phase, deadline=deadline)) if self._monitor else None
        try:
            tasks: list[asyncio.Task[Any]] = [task]
            if stopped is not None:
                tasks.append(stopped)
            if self._checkpoint_task is not None:
                tasks.append(self._checkpoint_task)
            timeout = None if deadline is None else max(0, deadline - time.monotonic())
            done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if self._checkpoint_task is not None and self._checkpoint_task in done:
                await self._checkpoint_task
            if task in done:
                await task
            if stopped is not None and stopped in done:
                reasons = stopped.result()
            else:
                reasons = self._monitor.reasons() if self._monitor else []
            return "+".join(reason.code for reason in reasons) or "COMPLETED"
        finally:
            if stopped is not None:
                stopped.cancel()
                await asyncio.gather(stopped, return_exceptions=True)

    async def _supervise(self, work: Awaitable[None]) -> str:
        """Run the active part of a pipeline until completion or a stop condition."""
        task = asyncio.ensure_future(work)
        if self._save is not None and self._checkpointing and self._checkpoint_task is None:
            self._checkpoint_task = asyncio.create_task(self._checkpoint_loop())
        try:
            # Register restored or newly dispatched work before checking exhaustion.
            await asyncio.sleep(0)
            reason = await self._wait_for_stop(task)
            self._stopping = True
            self._deadline = time.monotonic() + _SETTLE_TIMEOUT
            return reason
        except BaseException:
            await self._stop_checkpoints()
            raise
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _settle_step(self, work: Callable[[], Awaitable[None]], phase: str) -> None:
        """Keep all settling steps within one deadline, with live stopping checks."""
        if self._deadline is None:
            raise RuntimeError("settlement requires a completed supervision phase")
        if not self._allow_finish(phase) or time.monotonic() >= self._deadline:
            return
        task = asyncio.ensure_future(work())
        try:
            await self._wait_for_stop(task, phase=phase, deadline=self._deadline)
            if not task.done() and time.monotonic() >= self._deadline:
                logger.warning("%s.settle_timeout", phase)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._stop_checkpoints()
        try:
            if self._monitor is not None:
                self._monitor.close()
        finally:
            if self._pipeline is not None:
                await self._pipeline.aclose()

    async def _checkpoint(self) -> None:
        if not self._checkpointing or self._closed or self._save is None:
            return
        save = asyncio.ensure_future(self._save())
        self._checkpoint_saves.add(save)
        try:
            await save
        finally:
            self._checkpoint_saves.discard(save)

    async def _stop_checkpoints(self) -> None:
        self._checkpointing = False
        tasks = list(self._checkpoint_saves)
        if self._checkpoint_task is not None:
            tasks.append(self._checkpoint_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _checkpoint_loop(self) -> None:
        while True:
            await asyncio.sleep(_CHECKPOINT_INTERVAL)
            await self._checkpoint()
