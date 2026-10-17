"""Create a generic Engine and bind its supplied pipeline."""

from collections.abc import Awaitable, Callable
from typing import TypeVar

from crawlme.scheduler.engine.engine import Engine
from crawlme.scheduler.monitor import Monitor
from crawlme.scheduler.pipelines import Pipeline

Input = TypeVar("Input")
Output = TypeVar("Output")


def create_engine(
    pipeline: Pipeline[Input, Output],
    *,
    monitor: Monitor | None = None,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
) -> Engine[Input, Output]:
    engine: Engine[Input, Output] = Engine(monitor=monitor, checkpoint=checkpoint)
    engine.bind(pipeline)
    return engine
