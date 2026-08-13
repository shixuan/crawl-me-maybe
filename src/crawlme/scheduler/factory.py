"""Scheduler factory: the single place where concrete implementations are chosen.

Every concrete import lives here.  Engine itself depends only on Protocols.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from crawlme.config import Settings
from crawlme.digest.extractor import TrafExtractor
from crawlme.digest.fetcher import HttpFetcher
from crawlme.pioneer.buffer import InMemoryBuffer
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.pioneer.frontier import PriorityFrontier
from crawlme.pioneer.prefilter import PreFilter
from crawlme.pioneer.ranker import HybridRanker, RuleRanker
from crawlme.pioneer.ranker.embedding import (
    Embedder,
    EmbeddingRanker,
    FastEmbedEmbedder,
    OpenAICompatibleEmbedder,
)
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.scheduler.engine import CrawlScheduler
from crawlme.schemas import CrawlGoal
from crawlme.state.storage import SqliteEmbeddingCache, SqliteStorage


def create_scheduler(
    settings: Settings,
    goal: CrawlGoal | None = None,
    **overrides: Any,
) -> CrawlScheduler:
    """Create a fully-wired CrawlScheduler.

    *settings* holds every knob (env + flag overrides, see config.py);
    *goal* supplies the domain budget.  Pass keyword overrides to swap
    individual components in tests:
    ``create_scheduler(cfg, goal, fetcher=_MockFetcher())``.
    """
    storage = SqliteStorage.create(settings.result_dir)
    kwargs: dict[str, Any] = {
        "settings": settings,
        "storage": storage,
        "frontier": PriorityFrontier(domain_budget=goal.domain_budget if goal else 50),
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
        "ranker": _build_ranker(settings),
        "canonicalizer": Canonicalizer(),
    }
    kwargs.update(overrides)
    return CrawlScheduler(**kwargs)


_LOCAL_DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_API_DEFAULT_MODEL = "text-embedding-3-small"


def _build_ranker(settings: Settings) -> HybridRanker:
    """Wire the ranking pipeline according to settings.

    embedding_provider "" (--embedding off): pure v0.1 rule-only
    behavior (RuleRanker threshold 0.35 is the sole gate).

    Provider set (default local): rule stage stops dropping (threshold
    0) and only orders; EmbeddingRanker becomes the gate via top-K
    semantic selection.  Provider choice: local (fastembed ONNX) or
    api (OpenAI-compatible).  embedding_model overrides the per-
    provider default model.  Vectors persist model-scoped in a global
    SQLite cache under result_dir (results/embedding_cache.db),
    shared across tasks.
    """
    if not settings.embedding_provider:
        return HybridRanker()
    model = settings.embedding_model or (
        _LOCAL_DEFAULT_MODEL if settings.embedding_provider == "local" else _API_DEFAULT_MODEL
    )
    embedder: Embedder
    if settings.embedding_provider == "local":
        if importlib.util.find_spec("fastembed") is None:
            raise RuntimeError(
                "--embedding local requires the 'fastembed' package, which ships as a "
                "core dependency: reinstall with `pip install -e .`"
            )
        embedder = FastEmbedEmbedder(model=model)
    else:
        if not settings.embedding_base_url and not settings.embedding_api_key:
            raise RuntimeError(
                "--embedding api requires EMBEDDING_API_KEY "
                "(or set EMBEDDING_BASE_URL for a keyless endpoint such as a local Ollama)"
            )
        embedder = OpenAICompatibleEmbedder(
            model=model,
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
        )
    return HybridRanker(
        rule=RuleRanker(threshold=0.0),
        embedding=EmbeddingRanker(
            embedder,
            keep=settings.embedding_keep,
            cache=SqliteEmbeddingCache(Path(settings.result_dir) / "embedding_cache.db"),
        ),
    )
