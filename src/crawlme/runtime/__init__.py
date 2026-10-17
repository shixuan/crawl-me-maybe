"""Goals, tasks, run data, tracking and persisted events."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from crawlme.runtime.events import EventEmitter, EventType
from crawlme.runtime.goal import Goal, spec_fields, spec_time_field, spec_version
from crawlme.runtime.task import Task, TaskState

if TYPE_CHECKING:
    from crawlme.runtime.state import (
        RELEVANCE_WINDOW,
        Funnel,
        Limits,
        PageBook,
        PageRecord,
        Progress,
        RunState,
        SeedState,
        Stats,
    )
    from crawlme.runtime.tracking import MAX_STALE_STREAK, Tracker, why_retire

__all__ = [
    "MAX_STALE_STREAK",
    "RELEVANCE_WINDOW",
    "EventEmitter",
    "EventType",
    "Funnel",
    "Goal",
    "Limits",
    "PageBook",
    "PageRecord",
    "Progress",
    "RunState",
    "SeedState",
    "Stats",
    "Task",
    "TaskState",
    "Tracker",
    "spec_fields",
    "spec_time_field",
    "spec_version",
    "why_retire",
]


def __getattr__(name: str) -> Any:
    # Keep importing data types independent of loading their consumers.
    modules = {
        "RELEVANCE_WINDOW": "crawlme.runtime.state",
        "Funnel": "crawlme.runtime.state",
        "Limits": "crawlme.runtime.state",
        "PageBook": "crawlme.runtime.state",
        "PageRecord": "crawlme.runtime.state",
        "Progress": "crawlme.runtime.state",
        "RunState": "crawlme.runtime.state",
        "SeedState": "crawlme.runtime.state",
        "Stats": "crawlme.runtime.state",
        "MAX_STALE_STREAK": "crawlme.runtime.tracking",
        "Tracker": "crawlme.runtime.tracking",
        "why_retire": "crawlme.runtime.tracking",
    }
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(modules[name]), name)
    globals()[name] = value
    return value
