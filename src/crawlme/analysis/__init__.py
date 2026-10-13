"""Per-page relevance analysis and field extraction."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from crawlme.analysis.models import (
    CLASSIFICATIONS,
    AnalysisResult,
    AnalyzerFeedback,
    Classification,
    ExtractedField,
    ResultItem,
)

if TYPE_CHECKING:
    from crawlme.analysis.analyzer import Analyzer

__all__ = [
    "CLASSIFICATIONS",
    "AnalysisResult",
    "Analyzer",
    "AnalyzerFeedback",
    "Classification",
    "ExtractedField",
    "ResultItem",
]


def __getattr__(name: str) -> Any:
    # Keep importing data types independent of loading their consumers.
    modules = {
        "Analyzer": "crawlme.analysis.analyzer",
    }
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(modules[name]), name)
    globals()[name] = value
    return value
