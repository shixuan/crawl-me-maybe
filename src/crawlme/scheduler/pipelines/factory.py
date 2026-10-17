"""Select concrete implementations and assemble pipelines and their shared dependencies."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from crawlme.analysis import Analyzer
from crawlme.checkpoint import Checkpointer
from crawlme.config import Settings
from crawlme.dedup import Grouper
from crawlme.digest import TrafExtractor
from crawlme.digest.fetcher import DispatchingFetcher, Fetcher, HttpFetcher
from crawlme.discovery import Harvester
from crawlme.errors import ErrorHandler
from crawlme.llm import TokenBudget
from crawlme.persist import Storage
from crawlme.pioneer.buffer import RoundRobinBuffer
from crawlme.pioneer.frontier import Frontier
from crawlme.pioneer.goal_enhancer import GoalEnhancer
from crawlme.pioneer.prefilter import PreFilter
from crawlme.pioneer.ranker import LLMRanker, Ranker
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.pioneer.seed_expander import SeedExpander
from crawlme.platforms import ADAPTERS, FeedAdapter
from crawlme.runtime import Goal, Limits, Progress, RunState, Stats, Tracker
from crawlme.scheduler.pipelines.crawl import CrawlPipeline
from crawlme.scheduler.pipelines.dedup import DedupPipeline
from crawlme.scheduler.pipelines.page import PagePipeline
from crawlme.scheduler.pipelines.replay import ReplayError, ReplayPipeline
from crawlme.scheduler.workers import (
    AnalysisWorker,
    DiscoveryWorker,
    FetchWorker,
    PersistWorker,
    RankingWorker,
)
from crawlme.urls import Canonicalizer

logger = logging.getLogger(__name__)


def create_crawl(
    settings: Settings,
    goal: Goal | None = None,
    llm_ranker: Ranker | None = None,
    analyzer: Analyzer | None = None,
    *,
    budget: TokenBudget | None = None,
    frontier: Frontier | None = None,
    tracking: Tracker | None = None,
    dedup_enabled: bool = True,
    ranking_enabled: bool = False,
    **overrides: Any,
) -> CrawlPipeline:
    """Assemble crawl components with optional test overrides.

    A missing analyzer is built from settings when enabled and configured.
    The supplied token budget is shared with that analyzer."""
    budget = budget if budget is not None else TokenBudget(goal.max_tokens if goal else 0)
    state = overrides.pop("run_state", None)
    if tracking is None:
        state = state if state is not None else RunState(limits=Limits(), progress=Progress(), stats=Stats())
        tracking = Tracker(state)
    if frontier is None:
        frontier = Frontier(
            domain_budget=goal.domain_budget if goal else 50,
            buffer=RoundRobinBuffer(capacity=settings.candidate_buffer_size),
        )
    storage = overrides.pop("storage") if "storage" in overrides else Storage.create(settings.result_dir)
    canonicalizer = overrides.pop("canonicalizer", None) or Canonicalizer()
    fetcher = overrides.pop("fetcher") if "fetcher" in overrides else _build_fetcher(settings)
    extractor = overrides.pop("extractor", None) or TrafExtractor(adapters=adapters_for(settings))
    robots = overrides.pop("robots", None) or RobotsPolicy(agent=_agent_name(settings), ignore=settings.ignore_robots)
    harvester = overrides.pop("harvester") if "harvester" in overrides else _build_harvester(settings, canonicalizer)
    if ranking_enabled and llm_ranker is None:
        llm_ranker = LLMRanker.from_settings(settings, budget=budget)
    ranker = overrides.pop("ranker") if "ranker" in overrides else _build_ranker(settings, llm=llm_ranker)
    if ranker is not None:
        logger.info("ranking with the LLM")
    if "seed_expander" in overrides:
        seed_expander = overrides.pop("seed_expander")
    elif settings.expand_seeds:
        seed_expander = SeedExpander.from_settings(settings, budget=budget)
    else:
        seed_expander = None
    if analyzer is None and settings.analysis_enabled:
        analyzer = Analyzer.from_settings(settings, budget=budget)
    error_handler = overrides.pop("error_handler", None) or ErrorHandler()
    dedup = create_dedup(
        settings,
        storage=storage,
        budget=budget,
        enabled=dedup_enabled and settings.analysis_enabled,
        **({"grouper": overrides.pop("grouper")} if "grouper" in overrides else {}),
        error_handler=error_handler,
    )

    fetch = overrides.pop("fetch", None) or FetchWorker(
        fetcher,
        robots,
        storage,
        concurrency=settings.fetch_concurrency,
        error_handler=error_handler,
        max_attempts=settings.fetch_max_retries,
    )
    persist = overrides.pop("persist", None) or PersistWorker(storage)
    discovery = overrides.pop("discovery", None) or DiscoveryWorker(
        harvester, timeout=settings.extract_timeout, error_handler=error_handler
    )

    kwargs: dict[str, Any] = {
        "settings": settings,
        "error_handler": error_handler,
        "goal_enhancer": GoalEnhancer.from_settings(settings, budget=budget),
        "storage": storage,
        "frontier": frontier,
        "fetch": fetch,
        "persist": persist,
        "extractor": extractor,
        "discovery": discovery,
        "robots": robots,
        "prefilter": PreFilter(),
        "canonicalizer": canonicalizer,
        "tracking": tracking,
        "dedup": dedup,
        "seed_expander": seed_expander,
    }
    kwargs.update(overrides)
    if "analysis" not in kwargs:
        kwargs["analysis"] = AnalysisWorker(
            analyzer,
            concurrency=settings.llm_concurrency,
            storage=storage,
            tracking=kwargs["tracking"],
            error_handler=error_handler,
        )
    analysis = kwargs.pop("analysis")
    kwargs["ranking"] = RankingWorker(
        ranker, storage=storage, frontier=kwargs["frontier"], tracking=kwargs["tracking"], error_handler=error_handler
    )
    kwargs["page"] = PagePipeline(
        settings=settings,
        storage=storage,
        frontier=kwargs["frontier"],
        fetch=kwargs.pop("fetch"),
        persist=kwargs.pop("persist"),
        extractor=kwargs.pop("extractor"),
        analysis=analysis,
        discovery=kwargs.pop("discovery"),
        robots=robots,
        prefilter=kwargs["prefilter"],
        canonicalizer=canonicalizer,
        tracking=kwargs["tracking"],
    )
    checkpointer = Checkpointer(
        storage=kwargs["storage"],
        settings=settings,
        frontier=kwargs["frontier"],
        runtime=kwargs["tracking"].state,
        analysis=analysis,
        budget=budget,
        robots=kwargs["robots"],
        features={
            "dedup": kwargs["dedup"].grouper is not None,
            "ranking": kwargs["ranking"].ranker is not None,
            "analysis": analysis.analyzer is not None,
        },
    )
    return CrawlPipeline(
        **kwargs,
        budget=budget,
        checkpointer=checkpointer,
    )


def create_dedup(
    settings: Settings,
    *,
    storage: Storage,
    budget: TokenBudget,
    error_handler: ErrorHandler | None = None,
    enabled: bool = True,
    required: bool = False,
    **overrides: Any,
) -> DedupPipeline:
    """Assemble grouping with the caller's storage, budget and error policy."""
    if "grouper" in overrides:
        grouper = overrides.pop("grouper")
    else:
        grouper = Grouper.from_settings(settings, budget=budget) if enabled else None
    if required and grouper is None:
        raise ValueError("dedup needs LLM_API_KEY or LLM_BASE_URL")
    return DedupPipeline(
        grouper,
        storage=storage,
        model=settings.llm_model,
        error_handler=error_handler if error_handler is not None else ErrorHandler(),
    )


def create_replay(
    settings: Settings,
    *,
    storage: Storage,
    budget: TokenBudget,
    analyzer: Analyzer | None = None,
    dedup_enabled: bool = True,
) -> ReplayPipeline:
    """Assemble replay analysis and grouping with one budget and error handler."""
    if analyzer is None:
        analyzer = Analyzer.from_settings(settings, budget=budget)
        if analyzer is None:
            raise ReplayError(
                "replay needs LLM credentials to analyze pages: configure LLM_API_KEY / LLM_BASE_URL",
                code="llm_unconfigured",
            )
    error_handler = ErrorHandler()
    return ReplayPipeline(
        settings,
        storage=storage,
        budget=budget,
        analysis=AnalysisWorker(
            analyzer,
            concurrency=settings.llm_concurrency,
            storage=storage,
            error_handler=error_handler,
        ),
        enhancer=GoalEnhancer.from_settings(settings, budget=budget),
        dedup=create_dedup(
            settings,
            storage=storage,
            budget=budget,
            enabled=dedup_enabled,
            error_handler=error_handler,
        ),
    )


def _agent_name(settings: Settings) -> str:
    """Read the crawler product token from the first configured User-Agent."""
    first = next(iter(settings.user_agents), "")
    return first.split("/")[0].split()[0] or "*"


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
    """Return registered adapters in priority order, excluding session-required ones without a session."""
    has_session = bool(settings.browser_storage_state)
    return [a for a in ADAPTERS if has_session or not a.NEEDS_SESSION]


def _build_harvester(settings: Settings, canonicalizer: Canonicalizer) -> Harvester:
    return Harvester(canonicalizer, adapters=adapters_for(settings))


def _build_fetcher(settings: Settings) -> Fetcher:
    """Force browser fetching when configured; otherwise dispatch per URL."""
    if settings.fetcher == "browser":
        return _build_browser_fetcher(settings)
    return DispatchingFetcher(
        http=_build_http_fetcher(settings),
        browser=_build_browser_fetcher(settings),
        adapters=adapters_for(settings),
    )


def _build_http_fetcher(settings: Settings) -> Fetcher:
    return HttpFetcher(
        user_agents=list(settings.user_agents),
        connect_timeout=settings.fetch_timeout_connect,
        read_timeout=settings.fetch_timeout_read,
    )


def _build_browser_fetcher(settings: Settings) -> Fetcher:
    """Construct the lazy browser fetcher without importing or launching Playwright."""
    from crawlme.digest.fetcher import PlaywrightFetcher

    # Enable scrolling if any adapter requests it; settings supply the limit.
    scrolls = max((a.SCROLLS for a in adapters_for(settings)), default=0)
    return PlaywrightFetcher(
        storage_state=settings.browser_storage_state or None,
        user_agents=list(settings.user_agents),
        timeout=settings.fetch_timeout_read,
        keep_payload=_payload_filter(settings),
        max_payload_bytes=settings.browser_max_payload_bytes,
        scrolls=settings.feed_scrolls if scrolls else 0,
    )


def _build_ranker(settings: Settings, llm: Ranker | None = None) -> Ranker | None:
    """Return the supplied ranker, or None for unranked crawling."""
    return llm
