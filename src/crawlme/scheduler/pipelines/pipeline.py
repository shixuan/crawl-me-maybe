"""Execution contract shared by composed pipelines."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from crawlme.runtime import EventEmitter

Input = TypeVar("Input", contravariant=True)
Output = TypeVar("Output", covariant=True)


@dataclass(frozen=True)
class PipelineContext:
    task_id: str
    events: EventEmitter | None
    checkpoint: Callable[[], Awaitable[None]]
    can_dispatch: Callable[[], bool]
    can_run: Callable[[], bool]
    can_analyze: Callable[[], bool]


class Pipeline(Protocol[Input, Output]):
    @abstractmethod
    async def run(self, input: Input, context: PipelineContext) -> Output: ...

    @abstractmethod
    async def aclose(self) -> None: ...
