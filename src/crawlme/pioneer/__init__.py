"""Candidate filtering, ranking and scheduling."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from crawlme.pioneer.models import Candidate, CandidateStatus, FrontierItem, FrontierItemStatus

if TYPE_CHECKING:
    from crawlme.pioneer.frontier import Frontier, FrontierSnapshot
    from crawlme.pioneer.ranker import RankDecision, RankHistorySummary

__all__ = [
    "Candidate",
    "CandidateStatus",
    "Frontier",
    "FrontierItem",
    "FrontierItemStatus",
    "FrontierSnapshot",
    "RankDecision",
    "RankHistorySummary",
]


def __getattr__(name: str) -> Any:
    # Keep importing data types independent of loading their consumers.
    modules = {
        "Frontier": "crawlme.pioneer.frontier",
        "FrontierSnapshot": "crawlme.pioneer.frontier",
        "RankDecision": "crawlme.pioneer.ranker",
        "RankHistorySummary": "crawlme.pioneer.ranker",
    }
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(modules[name]), name)
    globals()[name] = value
    return value
