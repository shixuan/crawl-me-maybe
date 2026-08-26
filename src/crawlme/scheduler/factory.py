"""Scheduler factory: the single place where concrete implementations are chosen.

Every concrete import lives here.  Engine itself depends only on Protocols.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from crawlme.analyzer import Analyzer, PageAnalyzer
from crawlme.config import Settings
from crawlme.digest.extractor import TrafExtractor
from crawlme.digest.feed import ADAPTERS, FeedAdapter
from crawlme.digest.fetcher import DispatchingFetcher, Fetcher, HttpFetcher
from crawlme.digest.harvest import Harvester, PageHarvester
from crawlme.llm import TokenBudget
from crawlme.pioneer.buffer import RoundRobinBuffer
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.pioneer.frontier import GatedFrontier
from crawlme.pioneer.prefilter import PreFilter
from crawlme.pioneer.ranker import Ranker
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.scheduler.engine import CrawlScheduler
from crawlme.schemas import CrawlGoal
from crawlme.state.context import CrawlContext, CrawlCounters, RunStats
from crawlme.storage.sqlite.crawl_db import SqliteCrawlDb


def create_scheduler(
    settings: Settings,
    goal: CrawlGoal | None = None,
    llm_ranker: Ranker | None = None,
    analyzer: Analyzer | None = None,
    budget: TokenBudget | None = None,
    **overrides: Any,
) -> CrawlScheduler:
    """Create a fully-wired CrawlScheduler.

    *settings* holds every knob (env + flag overrides, see config.py);
    *goal* supplies the domain budget.  *llm_ranker* enables the v0.2
    LLM fine-ranking stage.  *analyzer* is the optional analysis stage
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
    if analyzer is None and settings.analysis_enabled:
        analyzer = PageAnalyzer.from_settings(settings, budget=budget)
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
        "ranker": _build_ranker(settings, llm=llm_ranker),
        "canonicalizer": canonicalizer,
        "harvester": _build_harvester(settings, canonicalizer),
        "analyzer": analyzer,
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
    """A browser where the run asked for one, and per candidate otherwise.

    Asking for one is a way to get it everywhere: a page belonging to no
    platform can still need a script run before it says anything, and
    only the person crawling it knows that.

    Holding a session is not that answer.  Credentials belong to the
    platform that issued them and mean nothing to the shop an analyser
    endorsed halfway through the run -- putting that shop through a
    browser buys a slower fetch of a page that would have answered a
    plain request.  Dispatching keeps the cookies where they apply: the
    platform's own addresses go through the browser that holds them.

    Neither fetcher costs anything to hold.  The browser launches on
    first use, so a crawl that never meets a platform never starts one.
    """
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
    """Constructed, not started.  Playwright is imported inside the
    fetcher's own first launch, so building one here costs nothing and
    needs no install; a run that dispatches to it only on Reddit links
    never pays unless it meets one.
    """
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


def _build_ranker(settings: Settings, llm: Ranker | None = None) -> Ranker | None:
    """The ranking stage, or None when there is nothing to rank with.

    One stage is left.  The rule and embedding stages were removed
    after seven crawls measured them: neither ever dropped a candidate,
    and neither ordered better than a coin flip on most tasks.  Without
    LLM credentials there is now no ranker at all, and the engine
    fetches in the order the frontier hands candidates out -- a turn
    from each seed, oldest first.
    """
    return llm
