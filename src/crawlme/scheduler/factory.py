"""Scheduler factory: the single place where concrete implementations are chosen.

Every concrete import lives here.  Engine itself depends only on Protocols.
"""

from __future__ import annotations

from typing import Any

from crawlme.config import Settings
from crawlme.digest.extractor import TrafExtractor
from crawlme.digest.fetcher import HttpFetcher
from crawlme.pioneer.buffer import InMemoryBuffer
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.pioneer.frontier import PriorityFrontier
from crawlme.pioneer.prefilter import PreFilter
from crawlme.pioneer.ranker import HybridRanker
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.scheduler.engine import CrawlScheduler
from crawlme.state.storage import SqliteStorage


def create_scheduler(settings: Settings, **overrides: Any) -> CrawlScheduler:
    """Create a fully-wired CrawlScheduler from a Settings object.

    Pass keyword overrides to swap individual components in tests:
    ``create_scheduler(cfg, fetcher=_MockFetcher())``.
    """
    kwargs: dict[str, Any] = {
        "settings": settings,
        "storage": SqliteStorage.create(settings.result_dir),
        "frontier": PriorityFrontier(domain_budget=settings.default_domain_budget),
        "fetcher": HttpFetcher(
            user_agents=list(settings.user_agents),
            connect_timeout=settings.fetch_timeout_connect,
            read_timeout=settings.fetch_timeout_read,
            max_retries=settings.fetch_max_retries,
        ),
        "extractor": TrafExtractor(),
        "robots": RobotsPolicy(ignore=settings.ignore_robots),
        "prefilter": PreFilter(),
        "buffer": InMemoryBuffer(capacity=settings.candidate_buffer_size),
        "ranker": HybridRanker(),
        "canonicalizer": Canonicalizer(),
    }
    kwargs.update(overrides)
    return CrawlScheduler(**kwargs)
