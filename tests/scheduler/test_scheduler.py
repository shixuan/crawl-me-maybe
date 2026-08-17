"""Unit tests for CrawlScheduler (mock all I/O, verify control flow)."""

from __future__ import annotations

import asyncio
import datetime
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.scheduler.engine import CrawlScheduler
from crawlme.schemas import (
    URL,
    AnalysisResult,
    AnalyzerFeedback,
    Candidate,
    CrawlGoal,
    CrawlTask,
    FetchResult,
    FrontierItem,
    Page,
)
from crawlme.state.context import CrawlCounters


def _goal(**kw) -> CrawlGoal:
    defaults: dict = dict(prompt="test", max_pages=5)
    defaults.update(kw)
    return CrawlGoal(**defaults)


def _task() -> CrawlTask:
    return CrawlTask(task_id="t1", state="CREATED")  # type: ignore[arg-type]


def _item() -> FrontierItem:
    url = URL(raw="https://example.com", canonical="https://example.com", url_key="k1", reg_domain="example.com")
    return FrontierItem(url=url, url_key="k1", priority=0.5, depth=0, reg_domain="example.com")


def _make_sched(**overrides) -> CrawlScheduler:
    """Build a scheduler with all-mock components for unit tests."""
    from crawlme.config import Settings

    buffer_mock = MagicMock()
    buffer_mock.wake = AsyncMock()
    buffer_mock.wait_until = AsyncMock()

    kwargs: dict = {
        "settings": Settings(),
        "storage": MagicMock(),
        "frontier": MagicMock(),
        "fetcher": MagicMock(aclose=AsyncMock()),
        "extractor": MagicMock(),
        "robots": MagicMock(),
        "prefilter": MagicMock(),
        "buffer": buffer_mock,
        "ranker": MagicMock(aclose=AsyncMock()),
        "canonicalizer": MagicMock(),
    }
    kwargs.update(overrides)
    return CrawlScheduler(**kwargs)  # type: ignore[arg-type]


def test_note_tokens_used_updates_counters():
    """The TokenBudget sink lands in the shared counters, which the
    BUDGET_TOKENS stop condition reads every pump iteration."""
    sched = _make_sched()
    sched.note_tokens_used(1234)
    assert sched._counters.tokens_used == 1234


@pytest.mark.asyncio
async def test_stops_when_frontier_empty():
    """Scheduler should stop immediately when frontier is empty and buffer empty."""
    sched = _make_sched()

    sched._state = "RUNNING"
    sched._goal = _goal(max_pages=5)
    sched._task = _task()
    sched._counters = CrawlCounters(
        max_pages=5,
        max_tokens=100000,
        max_duration_sec=3600,
        relevance_threshold=0.7,
    )

    # Mock frontier.pop_next to return None immediately.
    sched._frontier.pop_next = AsyncMock(return_value=None)

    await sched._fetch_pump()

    assert sched._state == "STOPPING"


@pytest.mark.asyncio
async def test_stops_on_budget_pages():
    """Scheduler should stop when pages_fetched reaches max_pages."""
    sched = _make_sched()

    sched._state = "RUNNING"
    sched._goal = _goal(max_pages=10)
    sched._task = _task()
    sched._counters = CrawlCounters(
        max_pages=10,
        pages_fetched=10,  # Already at budget.
        max_tokens=100000,
        max_duration_sec=3600,
        relevance_threshold=0.7,
    )

    await sched._fetch_pump()

    # BUDGET_PAGES fires immediately, so it never reaches pop_next.
    assert sched._state == "STOPPING"


@pytest.mark.asyncio
async def test_budget_gate_blocks_pops_while_inflight():
    """Committed budget (fetched + in-flight) must block new pops.

    Regression: the pump used to keep popping while fetches were in
    the air, overshooting max_pages by up to fetch_concurrency-1.
    """
    sched = _make_sched()
    sched._state = "RUNNING"
    sched._goal = _goal(max_pages=10)
    sched._task = _task()
    sched._counters = CrawlCounters(
        max_pages=10,
        pages_fetched=8,
        in_flight=2,  # 8 + 2 = 10 committed: nothing may be popped
    )

    pop_mock = AsyncMock(return_value=None)
    sched._frontier.pop_next = pop_mock

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sched._fetch_pump(), timeout=0.5)

    pop_mock.assert_not_called()


@pytest.mark.asyncio
async def test_budget_gate_allows_pops_under_budget():
    """Below budget, pops still happen (gate is a cap, not a stall)."""
    sched = _make_sched()
    sched._state = "RUNNING"
    sched._goal = _goal(max_pages=10)
    sched._task = _task()
    sched._counters = CrawlCounters(
        max_pages=10,
        pages_fetched=8,
        in_flight=1,  # 9 committed < 10: one more pop is allowed
    )

    pop_mock = AsyncMock(return_value=None)
    sched._frontier.pop_next = pop_mock
    sched._frontier.size = 0
    sched._buffer.is_empty = True

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sched._fetch_pump(), timeout=0.5)

    pop_mock.assert_called()


@pytest.mark.asyncio
async def test_rank_pump_exits_when_stopping():
    """Rank pump should exit when state transitions away from RUNNING."""
    sched = _make_sched()
    sched._state = "STOPPING"
    sched._goal = _goal()
    sched._counters = CrawlCounters()

    await sched._rank_pump()
    # Should exit immediately without error.
    assert sched._state == "STOPPING"


@pytest.mark.asyncio
async def test_pause_sets_state():
    """pause() should set state to PAUSED after in-flight tasks finish."""
    sched = _make_sched()
    sched._state = "RUNNING"
    sched._task = _task()
    sched._counters = CrawlCounters()

    # Mock checkpoint to avoid storage calls.
    sched._checkpoint = AsyncMock()

    await sched.pause()

    assert sched._state == "PAUSED"
    assert sched._task.state == "PAUSED"


@pytest.mark.asyncio
async def test_stop_sets_stopping():
    """stop() should set state to STOPPING."""
    sched = _make_sched()
    sched._state = "RUNNING"
    sched._task = _task()

    await sched.stop()

    assert sched._state == "STOPPING"
    assert sched._task.state == "STOPPING"


@pytest.mark.asyncio
async def test_aclose_closes_ranker_and_storage():
    """Shutdown must release stage-held resources (drain tasks, caches)."""
    ranker = MagicMock(aclose=AsyncMock())
    storage = MagicMock(close=AsyncMock())
    sched = _make_sched(ranker=ranker, storage=storage)
    await sched.aclose()
    ranker.aclose.assert_awaited_once()
    storage.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_aclose_closes_steering():
    """The steering facade flushes its prior DB; a leaked connection
    would keep the process alive (the aiosqlite worker-thread hang)."""
    steering = MagicMock(aclose=AsyncMock())
    ranker = MagicMock(aclose=AsyncMock())
    storage = MagicMock(close=AsyncMock())
    sched = _make_sched(steering=steering, ranker=ranker, storage=storage)
    await sched.aclose()
    steering.aclose.assert_awaited_once()


def test_on_analysis_feeds_steering_loop():
    """The analyzer sink is where analyses enter the steering loop."""
    from crawlme.steering.loop import SteeringLoop
    from crawlme.steering.signals import InflightSignals

    steering = SteeringLoop(analyzer=None, signals=InflightSignals())
    sched = _make_sched(steering=steering)
    result = AnalysisResult(
        page_id="p1",
        url_key="k1",
        feedback=AnalyzerFeedback(
            classification="RELEVANT",
            relevance_score=0.9,
            domain="example.com",
            url="https://example.com/x",
            title="X",
        ),
    )

    sched._on_analysis(result)

    summary = steering.summary()
    assert summary.pages_seen == 1
    assert summary.domain_priors["example.com"] == 0.9


def test_on_analysis_backfills_page_context():
    """2.9: the ranker reads the source page's verdict from here."""
    sched = _make_sched()
    sched._page_contexts["k1"] = {"title": "Existing", "link_count": 7}
    result = AnalysisResult(
        page_id="p1",
        url_key="k1",
        classification="RELEVANT",
        relevance_score=0.87,
        summary="Borrow checker deep dive.",
    )

    sched._on_analysis(result)

    ctx = sched._page_contexts["k1"]
    assert ctx["classification"] == "RELEVANT"
    assert ctx["relevance"] == 0.87
    assert ctx["summary"] == "Borrow checker deep dive."
    assert ctx["title"] == "Existing"
    assert ctx["link_count"] == 7


def test_page_context_write_preserves_earlier_verdict():
    """analyze runs before link extraction, so the later write must merge."""
    sched = _make_sched()
    sched._on_analysis(AnalysisResult(page_id="p1", url_key="k1", classification="HUB", relevance_score=0.4))

    sched._record_page_context("k1", {"title": "T", "link_count": 3})

    ctx = sched._page_contexts["k1"]
    assert ctx["classification"] == "HUB"
    assert ctx["title"] == "T"


def _page_published(when: datetime.datetime | None) -> Page:
    url = URL(raw="https://x.com/a", canonical="https://x.com/a", url_key="k1")
    return Page(url_key="k1", url=url, published_at=when)


def test_stale_streak_ignores_pages_without_since():
    sched = _make_sched()
    sched._counters.since = None
    sched._note_page_age(_page_published(datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)))
    assert sched._counters.stale_streak == 0


def test_stale_streak_advances_on_old_pages():
    sched = _make_sched()
    sched._counters.since = datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc)
    for _ in range(3):
        sched._note_page_age(_page_published(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)))
    assert sched._counters.stale_streak == 3


def test_stale_streak_resets_on_fresh_page():
    sched = _make_sched()
    sched._counters.since = datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc)
    sched._note_page_age(_page_published(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)))
    sched._note_page_age(_page_published(datetime.datetime(2026, 8, 15, tzinfo=datetime.timezone.utc)))
    assert sched._counters.stale_streak == 0


def test_stale_streak_untouched_by_undated_page():
    """Silence is not evidence, so it neither advances nor resets."""
    sched = _make_sched()
    sched._counters.since = datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc)
    sched._note_page_age(_page_published(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)))
    sched._note_page_age(_page_published(None))
    assert sched._counters.stale_streak == 1


def test_page_context_ignores_empty_url_key():
    sched = _make_sched()
    sched._record_page_context("", {"title": "T"})
    assert "" not in sched._page_contexts


def test_apply_steering_passes_through_without_store():
    sched = _make_sched()
    assert sched._apply_steering(0.5, None) == 0.5


def test_apply_steering_multiplies_hub_and_domain():
    """Hub pages boost their outlinks; a consistently relevant domain
    boosts all of its candidates.  Both multipliers stack."""
    from crawlme.steering.loop import SteeringLoop
    from crawlme.steering.signals import InflightSignals

    steering = SteeringLoop(analyzer=None, signals=InflightSignals())
    steering.update(
        AnalyzerFeedback(
            classification="AGGREGATOR",
            hub_score=0.9,
            domain="hub.com",
            url="https://hub.com/front",
        )
    )
    for _ in range(3):
        steering.update(AnalyzerFeedback(classification="RELEVANT", relevance_score=0.8, domain="good.com"))

    sched = _make_sched(steering=steering)
    sched._page_contexts["src1"] = {"url": "https://hub.com/front"}
    candidate = Candidate(
        url=URL(raw="https://good.com/x", canonical="https://good.com/x", url_key="x", reg_domain="good.com"),
        source_url_key="src1",
    )

    # 1.5 (hub) x 1.2 (domain) = 1.8
    assert sched._apply_steering(0.5, candidate) == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_inject_endorsed_pushes_priority_1_items():
    """Endorsed links skip ranking, resolve against their source page,
    and enter the frontier at full priority."""
    from crawlme.pioneer.canonicalizer import Canonicalizer
    from crawlme.pioneer.prefilter import Decision
    from crawlme.steering.loop import SteeringLoop
    from crawlme.steering.signals import InflightSignals

    steering = SteeringLoop(analyzer=None, signals=InflightSignals())
    steering.update(AnalyzerFeedback(url="https://src.com/page", endorsed_links=("https://a.com/x", "/rel")))
    sched = _make_sched(steering=steering, canonicalizer=Canonicalizer())
    sched._goal = _goal(max_pages=5)
    sched._page_contexts["src-key"] = {"depth": 2}
    sched._url_key_of["https://src.com/page"] = "src-key"
    sched._prefilter.check = MagicMock(return_value=(Decision.ALLOW, ""))
    sched._frontier.push_batch = AsyncMock()

    await sched._inject_endorsed()

    sched._frontier.push_batch.assert_awaited_once()
    items = sched._frontier.push_batch.call_args[0][0]
    assert len(items) == 2
    assert all(item.priority == 1.0 and item.score_source == "endorsed" for item in items)
    assert items[0].url.canonical == "https://a.com/x"
    assert items[1].url.canonical == "https://src.com/rel"  # relative link resolved
    assert items[0].depth == 3  # source depth 2 + 1


@pytest.mark.asyncio
async def test_inject_endorsed_respects_prefilter():
    """An endorsement never overrides the prefilter's hard rules."""
    from crawlme.pioneer.canonicalizer import Canonicalizer
    from crawlme.pioneer.prefilter import Decision
    from crawlme.steering.loop import SteeringLoop
    from crawlme.steering.signals import InflightSignals

    steering = SteeringLoop(analyzer=None, signals=InflightSignals())
    steering.update(AnalyzerFeedback(url="https://src.com/page", endorsed_links=("https://a.com/x",)))
    sched = _make_sched(steering=steering, canonicalizer=Canonicalizer())
    sched._goal = _goal(max_pages=5)
    sched._prefilter.check = MagicMock(return_value=(Decision.DROP, "dedup"))

    await sched._inject_endorsed()

    sched._frontier.push_batch.assert_not_called()


@pytest.mark.asyncio
async def test_link_extraction_timeout_drops_links_but_counts_page(monkeypatch):
    """A page whose link extraction hangs must not stall the crawl.

    The page still counts as fetched (it was fetched, extracted, and
    analyzed); only its link harvest is lost.  Regression for the
    unbounded extract_links call that could freeze the fetch pump on a
    pathological page.
    """
    done = threading.Event()

    def _slow_links(_page):
        done.wait(10)  # released by the test so the worker thread exits
        return []

    sched = _make_sched()
    sched._goal = _goal(max_pages=5)
    sched._task = _task()
    sched._counters = CrawlCounters(max_pages=5, max_tokens=100000, max_duration_sec=3600)
    sched._cfg.extract_timeout = 0.2
    sched._fetcher.fetch = AsyncMock(
        return_value=FetchResult(item_id="i1", url_key="k1", url=_item().url, raw=b"<html></html>")
    )
    sched._extractor.extract = MagicMock(
        return_value=Page(
            url_key="k1",
            url=URL(raw="https://example.com", canonical="https://example.com", url_key="k1"),
            title="slow page",
        )
    )
    monkeypatch.setattr("crawlme.scheduler.engine.extract_links", _slow_links)
    sched._frontier.record_outcome = AsyncMock()

    try:
        await sched._handle_fetch(_item())
    finally:
        done.set()

    assert sched._counters.pages_fetched == 1
    args = sched._frontier.record_outcome.call_args[0]
    assert args[1] == "COMPLETED"


def test_summary_reports_run_statistics():
    """summary() reads counters and stats straight from the context."""
    sched = _make_sched()
    sched._counters = CrawlCounters(pages_fetched=12, tokens_used=5000, started_at=100.0)
    sched._ctx.stats.links_discovered = 123
    sched._ctx.stats.candidates_ranked = 45
    sched._ctx.stats.fetch_errors = 2
    sched._ctx.stats.analyses_by_class = {"RELEVANT": 3, "IRRELEVANT": 1}
    sched._ctx.stats.embedding_cache_hits = 4
    sched._ctx.stats.embedding_cache_misses = 9

    summary = sched.summary()

    assert summary["pages_fetched"] == 12
    assert summary["tokens_used"] == 5000
    assert summary["candidates_discovered"] == 123
    assert summary["candidates_ranked"] == 45
    assert summary["fetch_errors"] == 2
    assert summary["analyses"] == {"RELEVANT": 3, "IRRELEVANT": 1}
    assert summary["embedding_cache_hits"] == 4
    assert summary["embedding_cache_misses"] == 9


def test_on_analysis_feeds_the_relevance_window():
    """The analyzer sink is the only writer DIMINISHING_RETURNS can have."""
    sched = _make_sched()
    sched._counters.relevance_threshold = 0.7

    sched._on_analysis(AnalysisResult(page_id="p1", url_key="k1", relevance_score=0.9))
    sched._on_analysis(AnalysisResult(page_id="p2", url_key="k2", relevance_score=0.2))

    assert list(sched._counters.relevance_window) == [True, False]


def test_relevance_window_uses_the_goal_threshold():
    """relevance_threshold stops being dead config here."""
    sched = _make_sched()
    sched._counters.relevance_threshold = 0.95

    sched._on_analysis(AnalysisResult(page_id="p1", url_key="k1", relevance_score=0.9))

    assert list(sched._counters.relevance_window) == [False]


@pytest.mark.asyncio
async def test_analysis_runs_outside_the_fetch_slot(monkeypatch):
    """Waiting on the LLM must not occupy fetch concurrency.

    Regression: analyze used to run inside the fetch semaphore, which made
    fetch_concurrency and llm_concurrency nested instead of independent.
    """
    from crawlme.config import Settings

    monkeypatch.setattr("crawlme.scheduler.engine.extract_links", lambda page: [])

    sched = _make_sched(settings=Settings(fetch_concurrency=1))
    sched._goal = _goal()
    sched._task = _task()

    url = URL(raw="https://x.com/a", canonical="https://x.com/a", url_key="k1")
    page = Page(url_key="k1", url=url)
    result = MagicMock(item_id="i1", status_code=200, raw=b"x")

    held: dict[str, bool] = {}

    async def _analyze(_page, _goal_arg):
        held["locked"] = sched._fetch_sem.locked()

    sched._steering = MagicMock(analyze=AsyncMock(side_effect=_analyze))
    sched._fetch_and_extract = AsyncMock(return_value=(result, page))
    sched._frontier.record_outcome = AsyncMock()
    sched._frontier.get_prefilter_context = MagicMock(return_value=MagicMock())
    sched._checkpoint = AsyncMock()

    await sched._handle_fetch(_item())

    assert held["locked"] is False


@pytest.mark.asyncio
async def test_fetch_slot_is_released_before_returning():
    """The slot covers the request and its parse, nothing longer."""
    from crawlme.config import Settings

    sched = _make_sched(settings=Settings(fetch_concurrency=1))
    sched._fetcher.fetch = AsyncMock(side_effect=RuntimeError("boom"))
    sched._frontier.record_outcome = AsyncMock()

    assert await sched._fetch_and_extract(_item()) is None
    assert not sched._fetch_sem.locked()
