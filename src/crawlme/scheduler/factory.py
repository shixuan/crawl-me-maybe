"""Select concrete implementations and assemble the crawl scheduler."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from crawlme.analysis import Analyzer
from crawlme.checkpoint import Checkpointer
from crawlme.config import Settings
from crawlme.dedup import Grouper
from crawlme.digest import TrafExtractor
from crawlme.digest.fetcher import DispatchingFetcher, Fetcher, HttpFetcher
from crawlme.discovery import Harvester
from crawlme.llm import TokenBudget
from crawlme.persist import Storage
from crawlme.pioneer.buffer import RoundRobinBuffer
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.pioneer.frontier import Frontier
from crawlme.pioneer.prefilter import PreFilter
from crawlme.pioneer.ranker import Ranker
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.platforms import ADAPTERS, FeedAdapter
from crawlme.runtime import Limits, Progress, RunState, Stats, Tracker
from crawlme.scheduler.engine import Engine
from crawlme.scheduler.monitor import StopMonitor
from crawlme.scheduler.pipelines.crawl import CrawlPipeline
from crawlme.scheduler.pipelines.page import PagePipeline
from crawlme.scheduler.workers import (
    AnalysisWorker,
    DedupWorker,
    DiscoveryWorker,
    FetchWorker,
    PersistWorker,
    RankingWorker,
)
from crawlme.schemas import CrawlGoal


def create_scheduler(
    settings: Settings,
    goal: CrawlGoal | None = None,
    llm_ranker: Ranker | None = None,
    analyzer: Analyzer | None = None,
    budget: TokenBudget | None = None,
    dedup_enabled: bool = True,
    **overrides: Any,
) -> Engine:
    """Assemble scheduler components with optional test overrides.

    A missing analyzer is built from settings when enabled and configured.
    The supplied token budget is shared with that analyzer."""
    storage = overrides.pop("storage") if "storage" in overrides else Storage.create(settings.result_dir)
    # The tracker and reporting use the same state across startup reset.
    state = overrides.pop("run_state", None)
    if state is None:
        state = RunState(limits=Limits(), progress=Progress(), stats=Stats())
    canonicalizer = overrides.pop("canonicalizer", None) or Canonicalizer()
    fetcher = overrides.pop("fetcher") if "fetcher" in overrides else _build_fetcher(settings)
    extractor = overrides.pop("extractor", None) or TrafExtractor(adapters=adapters_for(settings))
    robots = overrides.pop("robots", None) or RobotsPolicy(agent=_agent_name(settings), ignore=settings.ignore_robots)
    harvester = overrides.pop("harvester") if "harvester" in overrides else _build_harvester(settings, canonicalizer)
    ranker = overrides.pop("ranker") if "ranker" in overrides else _build_ranker(settings, llm=llm_ranker)
    budget = budget if budget is not None else TokenBudget(limit=goal.max_tokens if goal else 0)
    if analyzer is None and settings.analysis_enabled:
        analyzer = Analyzer.from_settings(settings, budget=budget)
    grouper = None
    if dedup_enabled and settings.analysis_enabled and (settings.llm_api_key or settings.llm_base_url):
        grouper = Grouper.from_settings(settings, budget=budget)

    fetch = overrides.pop("fetch", None) or FetchWorker(
        fetcher, robots, storage, concurrency=settings.fetch_concurrency
    )
    persist = overrides.pop("persist", None) or PersistWorker(storage)
    discovery = overrides.pop("discovery", None) or DiscoveryWorker(harvester, timeout=settings.extract_timeout)

    kwargs: dict[str, Any] = {
        "settings": settings,
        "storage": storage,
        "frontier": Frontier(
            domain_budget=goal.domain_budget if goal else 50,
            buffer=RoundRobinBuffer(capacity=settings.candidate_buffer_size),
        ),
        "fetch": fetch,
        "persist": persist,
        "extractor": extractor,
        "discovery": discovery,
        "robots": robots,
        "prefilter": PreFilter(),
        "canonicalizer": canonicalizer,
        "tracking": Tracker(state),
        "grouper": grouper,
    }
    kwargs.update(overrides)
    if "analysis" not in kwargs:
        kwargs["analysis"] = AnalysisWorker(
            analyzer, concurrency=settings.llm_concurrency, storage=storage, tracking=kwargs["tracking"]
        )
    state = kwargs["tracking"].state
    kwargs["frontier"].bind_changes(state.notify_change)
    budget.bind_changes(state.notify_change)
    state.notify_change()
    analysis = kwargs["analysis"]
    kwargs["dedup"] = DedupWorker(kwargs.pop("grouper"), storage=storage, model=settings.llm_model)
    kwargs["ranking"] = RankingWorker(ranker, storage=storage, frontier=kwargs["frontier"], tracking=kwargs["tracking"])
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
        analysis=kwargs["analysis"].analyzer,
        budget=budget,
        robots=kwargs["robots"],
    )
    return Engine(
        settings=settings,
        storage=storage,
        pipeline=CrawlPipeline(**kwargs),
        checkpointer=checkpointer,
        monitor=StopMonitor(state, kwargs["frontier"], budget),
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
        max_retries=settings.fetch_max_retries,
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
