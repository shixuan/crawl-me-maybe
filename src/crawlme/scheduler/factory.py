"""Select concrete implementations and assemble the crawl scheduler."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from crawlme.analysis import Analyzer, PageAnalyzer
from crawlme.config import Settings
from crawlme.digest.extractor import TrafExtractor
from crawlme.digest.fetcher import DispatchingFetcher, Fetcher, HttpFetcher
from crawlme.discovery.harvester import Harvester, PageHarvester
from crawlme.llm import TokenBudget
from crawlme.pioneer.buffer import RoundRobinBuffer
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.pioneer.frontier import GatedFrontier
from crawlme.pioneer.prefilter import PreFilter
from crawlme.pioneer.ranker import Ranker
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.pioneer.seed_enhancer import enhance
from crawlme.platforms import ADAPTERS, FeedAdapter
from crawlme.runtime.state import Limits, Progress, RunState, Stats
from crawlme.runtime.tracking import RunTracker
from crawlme.scheduler.engine import CrawlScheduler
from crawlme.scheduler.workers import AnalysisWorker, DiscoveryWorker, FetchWorker, RankingWorker
from crawlme.schemas import Candidate, CrawlGoal
from crawlme.storage.sqlite.crawl_db import SqliteCrawlDb


def create_scheduler(
    settings: Settings,
    goal: CrawlGoal | None = None,
    llm_ranker: Ranker | None = None,
    analyzer: Analyzer | None = None,
    budget: TokenBudget | None = None,
    **overrides: Any,
) -> CrawlScheduler:
    """Assemble scheduler components with optional test overrides.

    A missing analyzer is built from settings when enabled and configured.
    The supplied token budget is shared with that analyzer."""
    storage = overrides.pop("storage") if "storage" in overrides else SqliteCrawlDb.create(settings.result_dir)
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
    if analyzer is None and settings.analysis_enabled:
        analyzer = PageAnalyzer.from_settings(settings, budget=budget)

    async def enhance_seeds(
        goal: CrawlGoal,
        seeds: list[str],
        budget: TokenBudget | None,
    ) -> tuple[list[Candidate], int, list[tuple[str, str]]]:
        return await enhance(
            goal,
            seeds,
            settings=settings,
            budget=budget,
            fetcher=fetcher,
            harvester=harvester,
            storage=storage,
            canonicalizer=canonicalizer,
        )

    kwargs: dict[str, Any] = {
        "settings": settings,
        "storage": storage,
        "frontier": GatedFrontier(
            domain_budget=goal.domain_budget if goal else 50,
            buffer=RoundRobinBuffer(capacity=settings.candidate_buffer_size),
        ),
        "fetch": FetchWorker(
            fetcher,
            extractor,
            robots,
            storage,
            concurrency=settings.fetch_concurrency,
            extract_timeout=settings.extract_timeout,
        ),
        "ranking": RankingWorker(ranker),
        "analysis": AnalysisWorker(analyzer, concurrency=settings.llm_concurrency),
        "discovery": DiscoveryWorker(harvester, timeout=settings.extract_timeout),
        "robots": robots,
        "prefilter": PreFilter(),
        "canonicalizer": canonicalizer,
        "tracking": RunTracker(state),
        "seed_enhancer": enhance_seeds,
    }
    kwargs.update(overrides)
    return CrawlScheduler(**kwargs)


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
    return PageHarvester(canonicalizer, adapters=adapters_for(settings))


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
