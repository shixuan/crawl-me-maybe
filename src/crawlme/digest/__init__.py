"""Fetch and extract page content."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from crawlme.digest.models import ExtractionStatus, FetchResult, Page, Payload

if TYPE_CHECKING:
    from crawlme.digest.extractor import Extractor, TrafExtractor
    from crawlme.digest.fetcher import DispatchingFetcher, Fetcher, FetchError, HttpFetcher, PlaywrightFetcher

__all__ = [
    "DispatchingFetcher",
    "ExtractionStatus",
    "Extractor",
    "FetchError",
    "FetchResult",
    "Fetcher",
    "HttpFetcher",
    "Page",
    "Payload",
    "PlaywrightFetcher",
    "TrafExtractor",
]


def __getattr__(name: str) -> Any:
    # Keep importing data types independent of loading their consumers.
    modules = {
        "Extractor": "crawlme.digest.extractor",
        "TrafExtractor": "crawlme.digest.extractor",
        "DispatchingFetcher": "crawlme.digest.fetcher",
        "Fetcher": "crawlme.digest.fetcher",
        "FetchError": "crawlme.digest.fetcher",
        "HttpFetcher": "crawlme.digest.fetcher",
        "PlaywrightFetcher": "crawlme.digest.fetcher",
    }
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(modules[name]), name)
    globals()[name] = value
    return value
