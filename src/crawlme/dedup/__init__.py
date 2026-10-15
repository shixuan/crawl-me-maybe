"""Group equivalent results while preserving every source analysis."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from crawlme.dedup.models import Group, Grouping

if TYPE_CHECKING:
    from crawlme.dedup.grouper import DedupError, Grouper

__all__ = ["DedupError", "Group", "Grouper", "Grouping"]


def __getattr__(name: str) -> Any:
    # Keep importing data types independent of loading their consumers.
    modules = {
        "Grouper": "crawlme.dedup.grouper",
        "DedupError": "crawlme.dedup.grouper",
    }
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(modules[name]), name)
    globals()[name] = value
    return value
