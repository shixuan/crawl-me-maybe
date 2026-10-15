"""Execution contract shared by standalone and composed pipelines."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

Input = TypeVar("Input", contravariant=True)
Output = TypeVar("Output", covariant=True)


@dataclass(frozen=True)
class PipelineContext:
    checkpoint: Callable[[], Awaitable[None]]
    stop_checkpoints: Callable[[], Awaitable[None]]
    supervise: Callable[[Awaitable[None]], Awaitable[str]]
    can_start: Callable[[str], bool]
    can_finish: Callable[[str], bool]
    settle: Callable[[Callable[[], Awaitable[None]], str], Awaitable[None]]


class Pipeline(Protocol[Input, Output]):
    async def run(self, input: Input, context: PipelineContext) -> Output: ...

    async def aclose(self) -> None: ...
