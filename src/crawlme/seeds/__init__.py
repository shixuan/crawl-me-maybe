"""Seed inputs, platform search and verified source expansion."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from crawlme.seeds.expander import SeedExpander
    from crawlme.seeds.proposals import Expansion, SeedProbe, SeedProposer
    from crawlme.seeds.search import SeedSearch

__all__ = ["Expansion", "SeedExpander", "SeedProbe", "SeedProposer", "SeedSearch"]


def __getattr__(name: str) -> Any:
    # Reading manual seeds does not need to load LLM components.
    modules = {
        "Expansion": "crawlme.seeds.proposals",
        "SeedExpander": "crawlme.seeds.expander",
        "SeedProbe": "crawlme.seeds.proposals",
        "SeedProposer": "crawlme.seeds.proposals",
        "SeedSearch": "crawlme.seeds.search",
    }
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(modules[name]), name)
    globals()[name] = value
    return value
