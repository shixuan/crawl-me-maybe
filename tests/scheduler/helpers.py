"""Shared scheduler fixtures with in-memory component doubles."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

from crawlme.config import Settings
from crawlme.discovery import Harvester
from crawlme.llm import TokenBudget
from crawlme.pioneer.frontier import Frontier
from crawlme.runtime import Limits, Progress, RunState, Stats
from crawlme.scheduler.engine import Engine
from crawlme.scheduler.factory import create_scheduler
from crawlme.scheduler.monitor import StopMonitor
from crawlme.scheduler.pipelines import PipelineContext
from crawlme.schemas import (
    URL,
    AnalysisResult,
    AnalyzerFeedback,
    CrawlGoal,
    CrawlTask,
    FetchResult,
    FrontierItem,
    Page,
)


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _goal(**kw) -> CrawlGoal:
    defaults: dict = dict(prompt="test", max_pages=5)
    defaults.update(kw)
    return CrawlGoal(**defaults)


def _task() -> CrawlTask:
    return CrawlTask(task_id="t1", state="CREATED")  # type: ignore[arg-type]


def _item() -> FrontierItem:
    url = URL(raw="https://example.com", canonical="https://example.com", url_key="k1", reg_domain="example.com")
    return FrontierItem(url=url, url_key="k1", priority=0.5, depth=0, reg_domain="example.com")


_LIMIT_FIELDS = {
    "max_pages",
    "max_tokens",
    "max_duration_sec",
    "max_relevant",
    "relevance_threshold",
    "recall",
    "since",
}


def _ctx(**kw) -> RunState:
    """A run context with each field routed to the half that owns it."""
    lim = {k: v for k, v in kw.items() if k in _LIMIT_FIELDS}
    prog = {k: v for k, v in kw.items() if k not in _LIMIT_FIELDS and k != "tokens_used"}
    state = RunState(limits=Limits(**lim), progress=Progress(**prog), stats=Stats())
    return state


def _set_context(sched, **kw):
    state = _ctx(**kw)
    budget = TokenBudget(limit=state.limits.max_tokens)
    budget.record(kw.get("tokens_used", 0), 0)
    sched._checkpointer.budget = budget
    sched.pipeline._tracking.state = state
    sched._monitor.close()
    sched._monitor = StopMonitor(state, sched.pipeline._frontier, budget)


def _make_sched(**overrides) -> Engine:
    """Build a scheduler with all-mock components for unit tests."""
    frontier_mock = MagicMock(spec=Frontier)
    frontier_mock.wake_ranker = AsyncMock()
    frontier_mock.wait_for_ranking = AsyncMock()
    frontier_mock.take_for_ranking = AsyncMock(return_value=[])
    frontier_mock.push_candidates = AsyncMock()
    # Counts, not auto-attributes: the pumps compare them to zero.
    frontier_mock.size = 0
    frontier_mock.waiting_size = 0
    frontier_mock.scoring = 0
    frontier_mock.cooling = 0
    frontier_mock.blocked_by_domain_budget = 0

    storage = MagicMock()
    # Async in the protocol, and the fetch path awaits it before every
    # first request to a domain.
    storage.get_robots = AsyncMock(return_value=None)
    storage.flush = AsyncMock()

    kwargs: dict = {
        "settings": Settings(),
        "storage": storage,
        "frontier": frontier_mock,
        "fetcher": MagicMock(aclose=AsyncMock()),
        "extractor": MagicMock(),
        "robots": MagicMock(next_allowed_at=MagicMock(return_value=_utcnow())),
        "prefilter": MagicMock(),
        "ranker": MagicMock(aclose=AsyncMock()),
        "canonicalizer": MagicMock(),
    }
    kwargs.update(overrides)
    settings = kwargs.pop("settings")
    if overrides.get("analyzer") is None:
        settings.analysis_enabled = False
    kwargs.setdefault("harvester", Harvester(kwargs["canonicalizer"]))
    scheduler = create_scheduler(settings, **kwargs)
    scheduler._task = _task()
    scheduler.pipeline._context = PipelineContext(
        task_id="t1",
        events=None,
        checkpoint=AsyncMock(),
        can_run=scheduler._allow_work,
        can_dispatch=scheduler._allow_dispatch,
        can_analyze=scheduler._allow_analysis,
    )
    scheduler.pipeline._task_id = "t1"
    return scheduler


def _page_published(when: datetime.datetime | None) -> Page:
    url = URL(raw="https://x.com/a", canonical="https://x.com/a", url_key="k1")
    return Page(url_key="k1", url=url, published_at=when)


_SINCE = datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc)


_STALE = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


_FRESH = datetime.datetime(2026, 8, 15, tzinfo=datetime.timezone.utc)


def _url(raw: str) -> URL:
    return URL(raw=raw, canonical=raw, url_key=raw, reg_domain="reddit.com")


def _paging_sched(**overrides):
    """A scheduler whose prefilter allows and whose frontier records."""
    frontier = MagicMock()
    frontier.push_batch = AsyncMock()
    prefilter = MagicMock()
    prefilter.check.return_value = (MagicMock(value="allow"), "")
    canon = MagicMock()
    canon.canonicalize.side_effect = lambda raw, _base: _url(raw)
    sched = _make_sched(frontier=frontier, prefilter=prefilter, canonicalizer=canon, **overrides)
    sched.pipeline._goal = _goal(max_pages=100)
    return sched, frontier


def _robots_sched(raw: str, *, cached=None):
    """A scheduler whose fetcher answers robots.txt with *raw*."""
    from crawlme.pioneer.robots import RobotsPolicy

    storage = MagicMock()
    storage.get_robots = AsyncMock(return_value=cached)
    fetcher = MagicMock(aclose=AsyncMock())
    fetcher.fetch = AsyncMock(
        side_effect=lambda it: FetchResult(
            item_id=it.item_id, url=it.url, url_key=it.url_key, status=200, raw=raw.encode()
        )
    )
    frontier = MagicMock()
    frontier.record_outcome = AsyncMock()
    sched = _make_sched(
        storage=storage,
        fetcher=fetcher,
        frontier=frontier,
        robots=RobotsPolicy(agent="crawl-me-maybe"),
    )
    sched.pipeline._goal = _goal(max_pages=5)
    return sched, storage, fetcher


def _judge(sched, url_key, score):
    sched.pipeline._analysis.apply(
        AnalysisResult(
            url_key=url_key,
            classification="RELEVANT" if score >= 0.7 else "IRRELEVANT",
            relevance_score=score,
            feedback=AnalyzerFeedback(url=f"https://x.test/{url_key}", title="t"),
        )
    )


def _vote(sched, seed, url_key, relevant):
    sched.run_state.pages.open(url_key, f"https://x.test/{url_key}", seed)
    sched.run_state.pages.of(url_key).relevant = relevant
    sched.run_state.pages.of(url_key).listing = False
    sched.pipeline._page.vote(url_key)
