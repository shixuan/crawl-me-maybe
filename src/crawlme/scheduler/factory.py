"""Scheduler factory: the single place where concrete implementations are chosen.

Every concrete import lives here.  Engine itself depends only on Protocols.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import Any

from crawlme.analyzer import PageAnalyzer
from crawlme.config import Settings
from crawlme.digest.extractor import TrafExtractor
from crawlme.digest.feed import ADAPTERS, FeedAdapter
from crawlme.digest.fetcher import Fetcher, HttpFetcher
from crawlme.digest.harvest import Harvester, PageHarvester
from crawlme.llm import TokenBudget
from crawlme.pioneer.buffer import RoundRobinBuffer
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.pioneer.frontier import GatedFrontier
from crawlme.pioneer.prefilter import PreFilter
from crawlme.pioneer.ranker import HybridRanker, Ranker, RuleRanker
from crawlme.pioneer.ranker.embedding import (
    _DEFAULT_LOCAL_MODEL,
    Embedder,
    EmbeddingRanker,
    FastEmbedEmbedder,
    OpenAICompatibleEmbedder,
)
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.scheduler.engine import CrawlScheduler
from crawlme.schemas import CrawlGoal
from crawlme.state.context import CrawlContext, CrawlCounters, RunStats
from crawlme.steering import InflightSignals, SteeringLoop, SteeringSystem
from crawlme.storage.sqlite.crawl_db import SqliteCrawlDb
from crawlme.storage.sqlite.domain_prior import SqliteDomainPrior
from crawlme.storage.sqlite.embedding_cache import SqliteEmbeddingCache


def create_scheduler(
    settings: Settings,
    goal: CrawlGoal | None = None,
    llm_ranker: Ranker | None = None,
    steering: SteeringSystem | None = None,
    budget: TokenBudget | None = None,
    **overrides: Any,
) -> CrawlScheduler:
    """Create a fully-wired CrawlScheduler.

    *settings* holds every knob (env + flag overrides, see config.py);
    *goal* supplies the domain budget.  *llm_ranker* enables the v0.2
    LLM fine-ranking stage.  *steering* is the optional steering half
    of the feedback subsystem; None (the default) means "build it from
    settings", so it exists whenever analysis_enabled is on and
    degrades when no LLM credentials are configured.  Tests pass an
    explicit stub instead.  *budget* is the shared token budget the
    subsystem's analyzer must respect.  Pass keyword overrides to swap
    individual components in tests:
    ``create_scheduler(cfg, goal, fetcher=_MockFetcher())``.
    """
    storage = SqliteCrawlDb.create(settings.result_dir)
    # The run context: one mutable object that every stage records
    # into (stop-condition counters + report statistics).  The engine
    # resets it in place when run() starts, so the references handed
    # out here stay valid for the scheduler's lifetime.
    ctx = CrawlContext(counters=CrawlCounters(), stats=RunStats())
    canonicalizer = Canonicalizer()
    if steering is None:
        steering = _build_steering(settings, budget)
    kwargs: dict[str, Any] = {
        "settings": settings,
        "storage": storage,
        "frontier": GatedFrontier(
            domain_budget=goal.domain_budget if goal else 50,
            buffer=RoundRobinBuffer(capacity=settings.candidate_buffer_size),
        ),
        "fetcher": _build_fetcher(settings),
        "extractor": TrafExtractor(),
        "robots": RobotsPolicy(ignore=settings.ignore_robots),
        "prefilter": PreFilter(),
        "ranker": _build_ranker(settings, llm=llm_ranker, stats=ctx.stats),
        "canonicalizer": canonicalizer,
        "harvester": _build_harvester(settings, canonicalizer),
        "steering": steering,
        "context": ctx,
    }
    kwargs.update(overrides)
    return CrawlScheduler(**kwargs)


def _payload_filter(settings: Settings) -> Callable[[str, str], bool] | None:
    """Keep a response if any enabled adapter wants it.

    Only the adapter knows which of a platform's own requests carries
    the posts, and a run can have more than one adapter now.
    """
    adapters = [a for a in adapters_for(settings) if a.keeps_payload is not None]
    if not adapters:
        return None
    return lambda url, ctype: any(a.keeps_payload(url, ctype) for a in adapters)


def adapters_for(settings: Settings) -> list[FeedAdapter]:
    """Which adapters this run may use, in the order they are asked.

    An adapter that needs a session is left out when there is none, and
    not to be tidy: without credentials it cannot read its platform, so
    claiming a page would hand back a login wall, and one login wall
    ends the whole run.  A link-graph crawl that merely touches such a
    platform would be killed by it.

    Everything else is always available.  A feed claims by the
    document's root element, so it cannot mistake an HTML page for one,
    and a crawl that reaches a feed should read it as a feed whatever it
    was started for.
    """
    has_session = bool(settings.browser_storage_state)
    return [a for a in ADAPTERS if has_session or not a.NEEDS_SESSION]


def _build_harvester(settings: Settings, canonicalizer: Canonicalizer) -> Harvester:
    return PageHarvester(canonicalizer, adapters=adapters_for(settings))


def _build_fetcher(settings: Settings) -> Fetcher:
    """Plain HTTP unless the run asks for a browser.

    Playwright is imported inside the browser branch so an http run never
    pays for the optional dependency, and so a missing install fails at
    the point that wanted it.
    """
    if settings.fetcher == "browser":
        from crawlme.digest.fetcher import PlaywrightFetcher

        # A feed adapter is the only thing that knows which of a page's
        # own requests carries the posts.  Without one, nothing is kept
        # and the browser behaves exactly as it did before.
        # However many the greediest enabled adapter asks for.  Nothing
        # to scroll on a page nobody claims, and scrolling costs only the
        # page's own next request.
        scrolls = max((a.SCROLLS for a in adapters_for(settings)), default=0)
        return PlaywrightFetcher(
            storage_state=settings.browser_storage_state or None,
            user_agents=list(settings.user_agents),
            timeout=settings.fetch_timeout_read,
            keep_payload=_payload_filter(settings),
            max_payload_bytes=settings.browser_max_payload_bytes,
            scrolls=settings.feed_scrolls if scrolls else 0,
        )
    return HttpFetcher(
        user_agents=list(settings.user_agents),
        connect_timeout=settings.fetch_timeout_connect,
        read_timeout=settings.fetch_timeout_read,
        max_retries=settings.fetch_max_retries,
    )


def _build_steering(settings: Settings, budget: TokenBudget | None = None) -> SteeringSystem | None:
    """Wire the steering half of the feedback subsystem, or return None.

    analysis_enabled off means nothing is built: the engine runs with
    the whole subsystem absent, analyzer included.  Enabled but without
    credentials, the analyzer degrades away while the prior store
    stays, so past tasks' domain reputation still informs the rule
    ranker's F4 factor.
    """
    if not settings.analysis_enabled:
        return None
    analyzer = PageAnalyzer.from_settings(settings, budget=budget)
    prior_store = SqliteDomainPrior(Path(settings.result_dir) / "feedback.db")
    return SteeringLoop(analyzer=analyzer, signals=InflightSignals(prior_store), prior_store=prior_store)


_API_DEFAULT_MODEL = "text-embedding-3-small"


def _build_ranker(settings: Settings, llm: Ranker | None = None, stats: RunStats | None = None) -> HybridRanker:
    """Wire the ranking pipeline according to settings.

    embedding_provider "" (--embedding off): pure v0.1 rule-only
    behavior (RuleRanker threshold 0.35 is the sole gate).  With an
    *llm* stage the rule threshold relaxes to 0.25: the coarse filter
    favors recall, because the LLM is the final gate and can correct
    its mistakes.

    Provider set (default local): rule stage stops dropping (threshold
    0) and only orders; EmbeddingRanker becomes the gate via top-K
    semantic selection.  Provider choice: local (fastembed ONNX) or
    api (OpenAI-compatible).  embedding_model overrides the per-
    provider default model.  Vectors persist model-scoped in a global
    SQLite cache under result_dir (results/embedding_cache.db),
    shared across tasks.  An *llm* stage fine-ranks the embedding
    survivors.
    """
    if not settings.embedding_provider:
        if llm is not None:
            return HybridRanker(rule=RuleRanker(threshold=0.25), llm=llm)
        return HybridRanker()
    model = settings.embedding_model or (
        _DEFAULT_LOCAL_MODEL if settings.embedding_provider == "local" else _API_DEFAULT_MODEL
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
            max_batch=settings.embedding_batch_size,
        )
    return HybridRanker(
        # A feed post has no anchor, no path shape and no position in a
        # page, and every post shares one domain: the graph set would
        # score five of its seven factors on constants.
        rule=RuleRanker(threshold=0.0),
        embedding=EmbeddingRanker(
            embedder,
            keep=settings.embedding_keep,
            cache=SqliteEmbeddingCache(Path(settings.result_dir) / "embedding_cache.db"),
            stats=stats,
            demote_dropped=settings.recall,
        ),
        llm=llm,
    )
