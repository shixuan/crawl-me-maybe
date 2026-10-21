"""Select concrete implementations and assemble pipelines and their shared dependencies."""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import Enum, auto

from crawlme.analysis import Analyzer
from crawlme.checkpoint import Checkpointer
from crawlme.config import Settings
from crawlme.dedup import Grouper
from crawlme.digest import Extractor, TrafExtractor
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


class _Default(Enum):
    AUTO = auto()


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
    storage: Storage | None = None,
    run_state: RunState | None = None,
    error_handler: ErrorHandler | None = None,
    canonicalizer: Canonicalizer | None = None,
    fetcher: Fetcher | None = None,
    extractor: Extractor | None = None,
    robots: RobotsPolicy | None = None,
    harvester: Harvester | None = None,
    prefilter: PreFilter | None = None,
    fetch: FetchWorker | None = None,
    persist: PersistWorker | None = None,
    discovery: DiscoveryWorker | None = None,
    analysis: AnalysisWorker | None = None,
    ranker: Ranker | _Default | None = _Default.AUTO,
    seed_expander: SeedExpander | _Default | None = _Default.AUTO,
    grouper: Grouper | _Default | None = _Default.AUTO,
    goal_enhancer: GoalEnhancer | None = None,
    dedup: DedupPipeline | None = None,
) -> CrawlPipeline:
    """Assemble crawl dependencies; explicit None disables ranker, seed_expander and grouper."""
    if tracking is not None and run_state is not None and tracking.state is not run_state:
        raise ValueError("run_state must be tracking.state when both are supplied")
    budget = budget if budget is not None else TokenBudget(goal.max_tokens if goal else 0)
    if tracking is None:
        run_state = (
            run_state if run_state is not None else RunState(limits=Limits(), progress=Progress(), stats=Stats())
        )
        tracking = Tracker(run_state)
    if frontier is None:
        frontier = Frontier(
            domain_budget=goal.domain_budget if goal else 50,
            buffer=RoundRobinBuffer(capacity=settings.candidate_buffer_size),
        )
    if storage is None:
        storage = Storage.create(settings.result_dir)
    if canonicalizer is None:
        canonicalizer = Canonicalizer()
    if robots is None:
        robots = RobotsPolicy(agent=_agent_name(settings), ignore=settings.ignore_robots)
    if prefilter is None:
        prefilter = PreFilter()
    if error_handler is None:
        error_handler = ErrorHandler()
    if isinstance(ranker, _Default):
        if ranking_enabled and llm_ranker is None:
            llm_ranker = LLMRanker.from_settings(settings, budget=budget)
        ranker = _build_ranker(settings, llm=llm_ranker)
    if ranker is not None:
        logger.info("ranking with the LLM")
    if isinstance(seed_expander, _Default):
        seed_expander = SeedExpander.from_settings(settings, budget=budget) if settings.expand_seeds else None
    if goal_enhancer is None:
        goal_enhancer = GoalEnhancer.from_settings(settings, budget=budget)
    if dedup is None:
        dedup = create_dedup(
            settings,
            storage=storage,
            budget=budget,
            enabled=dedup_enabled and settings.analysis_enabled,
            grouper=grouper,
            error_handler=error_handler,
        )
    if fetch is None:
        if fetcher is None:
            fetcher = _build_fetcher(settings)
        fetch = FetchWorker(
            fetcher,
            robots,
            storage,
            concurrency=settings.fetch_concurrency,
            error_handler=error_handler,
            max_attempts=settings.fetch_max_retries,
        )
    if persist is None:
        persist = PersistWorker(storage)
    if extractor is None:
        extractor = TrafExtractor(adapters=adapters_for(settings))
    if discovery is None:
        if harvester is None:
            harvester = _build_harvester(settings, canonicalizer)
        discovery = DiscoveryWorker(harvester, timeout=settings.extract_timeout, error_handler=error_handler)
    if analysis is None:
        if analyzer is None and settings.analysis_enabled:
            analyzer = Analyzer.from_settings(settings, budget=budget)
        analysis = AnalysisWorker(
            analyzer,
            concurrency=settings.llm_concurrency,
            storage=storage,
            tracking=tracking,
            error_handler=error_handler,
        )
    ranking = RankingWorker(ranker, storage=storage, frontier=frontier, tracking=tracking, error_handler=error_handler)
    page = PagePipeline(
        settings=settings,
        storage=storage,
        frontier=frontier,
        fetch=fetch,
        persist=persist,
        extractor=extractor,
        analysis=analysis,
        discovery=discovery,
        robots=robots,
        prefilter=prefilter,
        canonicalizer=canonicalizer,
        tracking=tracking,
    )
    checkpointer = Checkpointer(
        storage=storage,
        settings=settings,
        frontier=frontier,
        runtime=tracking.state,
        analysis=analysis,
        budget=budget,
        robots=robots,
        features={
            "dedup": dedup.grouper is not None,
            "ranking": ranking.ranker is not None,
            "analysis": analysis.analyzer is not None,
        },
    )
    return CrawlPipeline(
        settings=settings,
        storage=storage,
        frontier=frontier,
        tracking=tracking,
        budget=budget,
        error_handler=error_handler,
        goal_enhancer=goal_enhancer,
        ranking=ranking,
        robots=robots,
        prefilter=prefilter,
        canonicalizer=canonicalizer,
        dedup=dedup,
        page=page,
        seed_expander=seed_expander,
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
    grouper: Grouper | _Default | None = _Default.AUTO,
) -> DedupPipeline:
    """Assemble grouping with the caller's storage, budget and error policy."""
    if isinstance(grouper, _Default):
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
