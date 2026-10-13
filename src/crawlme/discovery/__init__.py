"""Discover candidates from platform listings and ordinary page links."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from crawlme.discovery.models import Harvest

if TYPE_CHECKING:
    from crawlme.discovery.harvester import Harvester

__all__ = ["Harvest", "Harvester"]


def __getattr__(name: str) -> Any:
    # Keep importing data types independent of loading their consumers.
    modules = {
        "Harvester": "crawlme.discovery.harvester",
    }
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(modules[name]), name)
    globals()[name] = value
    return value
