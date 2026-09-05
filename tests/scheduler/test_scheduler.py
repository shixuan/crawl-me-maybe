"""Unit tests for CrawlScheduler (mock all I/O, verify control flow)."""

from __future__ import annotations

import asyncio
import datetime
import logging
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crawlme.config import Settings
from crawlme.digest.harvest import Harvest
from crawlme.scheduler.engine import CrawlScheduler, _endorsed_href
from crawlme.scheduler.stop_conds import MAX_STALE_STREAK, RELEVANCE_WINDOW
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


def _make_sched(**overrides) -> CrawlScheduler:
    """Build a scheduler with all-mock components for unit tests."""
    # The waiting half lives inside the frontier now, so the mock hangs
    # off it rather than beside it.
    frontier_mock = MagicMock()
    frontier_mock.waiting = MagicMock()
    frontier_mock.waiting.wake = AsyncMock()
    frontier_mock.waiting.wait_until = AsyncMock()
    frontier_mock.take_for_ranking = AsyncMock(return_value=[])
    frontier_mock.push_candidates = AsyncMock()
    # Counts, not auto-attributes: the pumps compare them to zero.
    frontier_mock.scoring = 0
    frontier_mock.cooling = 0

    storage = MagicMock()
    # Async in the protocol, and the fetch path awaits it before every
    # first request to a domain.
    storage.get_robots = AsyncMock(return_value=None)

    kwargs: dict = {
        "settings": Settings(),
        "storage": storage,
        "frontier": frontier_mock,
        "fetcher": MagicMock(aclose=AsyncMock()),
        "extractor": MagicMock(),
        "robots": MagicMock(),
        "prefilter": MagicMock(),
        "ranker": MagicMock(aclose=AsyncMock()),
        "canonicalizer": MagicMock(),
    }
    kwargs.update(overrides)
    return CrawlScheduler(**kwargs)  # type: ignore[arg-type]


def test_tokens_counted():
    """The TokenBudget sink lands in the shared counters, which the
    BUDGET_TOKENS stop condition reads every pump iteration."""
    sched = _make_sched()
    sched.note_tokens_used(1234)
    assert sched._counters.tokens_used == 1234


@pytest.mark.asyncio
async def test_stop_drained():
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
async def test_stop_pages():
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
async def test_gate_blocks():
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
async def test_gate_allows():
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
    sched._frontier.waiting.is_empty = True

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sched._fetch_pump(), timeout=0.5)

    pop_mock.assert_called()


@pytest.mark.asyncio
async def test_rank_pump_exits():
    """Rank pump should exit when state transitions away from RUNNING."""
    sched = _make_sched()
    sched._state = "STOPPING"
    sched._goal = _goal()
    sched._counters = CrawlCounters()

    await sched._rank_pump()
    # Should exit immediately without error.
    assert sched._state == "STOPPING"


@pytest.mark.asyncio
async def test_pause_state():
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
async def test_stop_state():
    """stop() should set state to STOPPING."""
    sched = _make_sched()
    sched._state = "RUNNING"
    sched._task = _task()

    await sched.stop()

    assert sched._state == "STOPPING"
    assert sched._task.state == "STOPPING"


@pytest.mark.asyncio
async def test_aclose():
    """Shutdown must release every stage-held resource.

    Each of these owns something that outlives the run: drain tasks and
    caches in the ranker, a retry queue in the analyzer.  A leaked
    aiosqlite connection keeps its worker thread, and the process hangs
    instead of exiting.
    """
    ranker = MagicMock(aclose=AsyncMock())
    storage = MagicMock(close=AsyncMock())
    analyzer = MagicMock(aclose=AsyncMock())
    sched = _make_sched(ranker=ranker, storage=storage, analyzer=analyzer)
    await sched.aclose()
    ranker.aclose.assert_awaited_once()
    storage.close.assert_awaited_once()
    analyzer.aclose.assert_awaited_once()


def test_keeps_endorsed():
    """The analyzer sink is where endorsed links enter the crawl."""
    sched = _make_sched()
    result = AnalysisResult(
        page_id="p1",
        url_key="k1",
        feedback=AnalyzerFeedback(
            classification="RELEVANT",
            relevance_score=0.9,
            domain="example.com",
            url="https://example.com/x",
            title="X",
            endorsed_links=("https://shop.example/promotions",),
        ),
    )

    sched._on_analysis(result)

    assert list(sched._endorsed) == [("https://shop.example/promotions", "https://example.com/x")]


def test_backfills_context():
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


def test_context_keeps_older():
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


_SINCE = datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc)
_STALE = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
_FRESH = datetime.datetime(2026, 8, 15, tzinfo=datetime.timezone.utc)


@pytest.mark.parametrize(
    ("since", "published", "expected"),
    [
        # No window asked for: the streak stays dormant whatever arrives.
        (None, [_STALE], 0),
        (_SINCE, [_STALE] * 3, 3),
        (_SINCE, [_STALE, _FRESH], 0),
        # Silence is not evidence: it neither advances nor resets.
        (_SINCE, [_STALE, None], 1),
    ],
)
def test_stale_streak(since, published, expected):
    """Per seed, because a feed is time-ordered per account and never as
    a whole. Counted globally it could only ever arm for one seed."""
    sched = _make_sched()
    sched._counters.since = since
    for at in published:
        sched._note_page_age(_page_published(at), "seedA")
    assert sched._tally_by_seed["seedA"].stale == expected


def test_context_needs_key():
    sched = _make_sched()
    sched._record_page_context("", {"title": "T"})
    assert "" not in sched._page_contexts


@pytest.mark.asyncio
async def test_endorsed_top():
    """Endorsed links skip ranking, resolve against their source page,
    and enter the frontier at full priority."""
    from crawlme.pioneer.canonicalizer import Canonicalizer
    from crawlme.pioneer.prefilter import Decision

    sched = _make_sched(canonicalizer=Canonicalizer())
    sched._endorsed.extend([("https://a.com/x", "https://src.com/page"), ("/rel", "https://src.com/page")])
    sched._goal = _goal(max_pages=5)
    sched._page_contexts["src-key"] = {"depth": 2}
    sched._pages.open("src-key", "https://src.com/page", "")
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
async def test_endorsed_drop():
    """An endorsement never overrides the prefilter's hard rules."""
    from crawlme.pioneer.canonicalizer import Canonicalizer
    from crawlme.pioneer.prefilter import Decision

    sched = _make_sched(canonicalizer=Canonicalizer())
    sched._endorsed.append(("https://a.com/x", "https://src.com/page"))
    sched._goal = _goal(max_pages=5)
    sched._prefilter.check = MagicMock(return_value=(Decision.DROP, "dedup"))

    await sched._inject_endorsed()

    sched._frontier.push_batch.assert_not_called()


@pytest.mark.asyncio
async def test_harvest_timeout(monkeypatch):
    """A page whose link extraction hangs must not stall the crawl.

    The page still counts as fetched (it was fetched, extracted, and
    analyzed); only its link harvest is lost.  Regression for the
    unbounded extract_links call that could freeze the fetch pump on a
    pathological page.
    """
    done = threading.Event()

    def _slow_links(_page, _depth):
        done.wait(10)  # released by the test so the worker thread exits
        return Harvest([])

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
    # The harvester is injected now, so a pathological page is
    # simulated by a slow harvest rather than a patched import.
    sched._harvester = MagicMock(harvest=_slow_links)
    sched._frontier.record_outcome = AsyncMock()

    try:
        await sched._handle_fetch(_item())
    finally:
        done.set()

    assert sched._counters.pages_fetched == 1
    args = sched._frontier.record_outcome.call_args[0]
    assert args[1] == "COMPLETED"


def test_summary_stats():
    """summary() reads counters and stats straight from the context."""
    sched = _make_sched()
    sched._counters = CrawlCounters(pages_fetched=12, tokens_used=5000, started_at=100.0)
    sched._ctx.stats.links_discovered = 123
    sched._ctx.stats.candidates_ranked = 45
    sched._ctx.stats.fetch_errors = 2
    sched._ctx.stats.analyses_by_class = {"RELEVANT": 3, "IRRELEVANT": 1}

    summary = sched.summary()

    assert summary["pages_fetched"] == 12
    assert summary["tokens_used"] == 5000
    assert summary["candidates_discovered"] == 123
    assert summary["candidates_ranked"] == 45
    assert summary["fetch_errors"] == 2
    assert summary["analyses"] == {"RELEVANT": 3, "IRRELEVANT": 1}


def test_window_fed():
    """A judged content page is what a source's window counts."""
    sched = _make_sched()
    sched._counters.relevance_threshold = 0.7

    for key, score in (("k1", 0.9), ("k2", 0.2)):
        sched._on_analysis(AnalysisResult(page_id="p", url_key=key, relevance_score=score))
        sched._pages.of(key).listing = False
        sched._cast_relevance_vote(key)

    assert list(sched._tally_by_seed[""].window) == [True, False]


def test_window_threshold():
    """relevance_threshold stops being dead config here."""
    sched = _make_sched()
    sched._counters.relevance_threshold = 0.95

    sched._on_analysis(AnalysisResult(page_id="p1", url_key="k1", relevance_score=0.9))
    sched._pages.of("k1").listing = False
    sched._cast_relevance_vote("k1")

    assert list(sched._tally_by_seed[""].window) == [False]


@pytest.mark.asyncio
async def test_analysis_free(monkeypatch):
    """Waiting on the LLM must not occupy fetch concurrency.

    Regression: analyze used to run inside the fetch semaphore, which made
    fetch_concurrency and llm_concurrency nested instead of independent.
    """
    sched = _make_sched(settings=Settings(fetch_concurrency=1))
    sched._harvester = MagicMock(harvest=lambda page, depth: Harvest([]))
    sched._goal = _goal()
    sched._task = _task()

    url = URL(raw="https://x.com/a", canonical="https://x.com/a", url_key="k1")
    page = Page(url_key="k1", url=url)
    result = MagicMock(item_id="i1", status_code=200, raw=b"x")

    held: dict[str, bool] = {}

    async def _analyze(_page, _goal_arg):
        held["locked"] = sched._fetch_sem.locked()

    sched._analyzer = MagicMock(analyze=AsyncMock(side_effect=_analyze))
    sched._fetch_and_extract = AsyncMock(return_value=(result, page))
    sched._frontier.record_outcome = AsyncMock()
    sched._frontier.get_prefilter_context = MagicMock(return_value=MagicMock())
    sched._checkpoint = AsyncMock()

    await sched._handle_fetch(_item())

    assert held["locked"] is False


@pytest.mark.asyncio
async def test_slot_released():
    """The slot covers the request and its parse, nothing longer."""
    sched = _make_sched(settings=Settings(fetch_concurrency=1))
    sched._fetcher.fetch = AsyncMock(side_effect=RuntimeError("boom"))
    sched._frontier.record_outcome = AsyncMock()

    assert await sched._fetch_and_extract(_item()) is None
    assert not sched._fetch_sem.locked()


@pytest.mark.asyncio
async def test_pump_quiet(caplog):
    """The rank pump is inside a rank call: it cannot act on a wake.

    Waking it every tick produced a line of log per tick for the whole
    length of the call, saying the buffer had items when it was empty.
    """
    sched = _make_sched()
    sched._state = "RUNNING"
    sched._goal = _goal()
    sched._task = _task()
    sched._counters = CrawlCounters()

    sched._frontier.scoring = 11
    sched._frontier.pop_next = AsyncMock(return_value=None)
    sched._frontier.size = 0
    sched._frontier.waiting.is_empty = True
    wake = AsyncMock()
    sched._frontier.waiting.wake = wake

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sched._fetch_pump(), timeout=0.3)

    wake.assert_not_called()
    assert "waking_rank" not in caplog.text


# endorsed links ---------------------------------------------------------


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://example.com/deals", "https://example.com/deals"),
        ("http://example.com/x", "http://example.com/x"),
        ("/promotions", "/promotions"),
        ("www.mollyteaca.com", "https://www.mollyteaca.com"),
        ("WWW.Example.COM", "https://WWW.Example.COM"),
    ],
)
def test_endorsed_kept(link, expected):
    assert _endorsed_href(link) == expected


@pytest.mark.parametrize("link", ["mollyteaca.com", "click here", "", "   ", "see our site"])
def test_endorsed_junk(link):
    """Resolving it against the page would fabricate a URL.

    Instagram answers 200 for any path, so the fabricated page looked
    like a successful fetch and cost an analysis and a page of budget.
    """
    assert _endorsed_href(link) is None


# end-of-run accounting ---------------------------------------------------


def test_unfinished_log(caplog):
    """Stopping early and finishing look identical from the outside.

    A missing session gave COMPLETED with no pages; a per-domain ceiling
    gave COMPLETED with a hundred and sixty candidates still queued; a
    rank batch landing after the last fetch gave COMPLETED for an account
    that was never opened. None of them said anything.
    """
    sched = _make_sched()
    sched._counters = CrawlCounters(pages_fetched=45)
    sched._frontier.size = 16
    sched._frontier.waiting_size = 4

    with caplog.at_level(logging.INFO):
        sched._reconcile()

    assert "task.reconcile" in caplog.text
    assert "task.unfinished" in caplog.text
    assert "20 candidates were never read" in caplog.text


def test_complete_quiet(caplog):
    sched = _make_sched()
    sched._counters = CrawlCounters(pages_fetched=10)
    sched._frontier.size = 0
    sched._frontier.waiting_size = 0

    with caplog.at_level(logging.INFO):
        sched._reconcile()

    assert "task.reconcile" in caplog.text
    assert "task.unfinished" not in caplog.text


def test_rank_drain_once():
    """Nothing in a drained batch is fetchable until all of it is scored.

    At 100 the ranker split the batch into nine calls of its own; the
    first was scored in thirty seconds and reached the frontier four and
    a half minutes later, after the run had stopped for lack of anything
    to fetch. The drain size is that latency, not a throughput knob.
    """
    from crawlme.pioneer.ranker.llm import _BATCH_SIZE
    from crawlme.scheduler.engine import _RANK_BATCH_SIZE

    assert _RANK_BATCH_SIZE <= _BATCH_SIZE, "a drain larger than one call reintroduces the wait"


def test_relevant_count():
    """One judgement answers two questions: the run's tally of what it
    found, and the source's own window of whether it is still paying."""
    sched = _make_sched()
    sched._counters = CrawlCounters(relevance_threshold=0.7)
    for key, score, cls in (("a", 0.9, "RELEVANT"), ("b", 0.2, "IRRELEVANT"), ("c", 0.75, "RELEVANT")):
        sched._on_analysis(AnalysisResult(url_key=key, relevance_score=score, classification=cls))
        sched._pages.of(key).listing = False
        sched._cast_relevance_vote(key)
    assert sched._counters.relevant_found == 2
    assert list(sched._tally_by_seed[""].window) == [True, False, True]


@pytest.mark.asyncio
async def test_cooldown_lives(caplog):
    """Nothing poppable right now is not the same as nothing left.

    A clock that stepped backwards on the host left the only seed with a
    cooldown in the future. The pop was refused, the pump read that as
    an exhausted frontier, and the run reported itself finished having
    fetched nothing at all.
    """
    sched = _make_sched()
    sched._state = "RUNNING"
    sched._goal = _goal()
    sched._task = _task()
    sched._counters = CrawlCounters()

    sched._frontier.pop_next = AsyncMock(return_value=None)
    sched._frontier.size = 1
    sched._frontier.cooling = 1
    sched._frontier.waiting.is_empty = True

    with caplog.at_level(logging.INFO):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sched._fetch_pump(), timeout=0.3)

    assert "fetch_pump.exhausted" not in caplog.text
    assert sched._task.stopping_reason is None


@pytest.mark.asyncio
async def test_refusal_stops():
    """The engine has to act on the difference, not just record it.

    Before this the harvester's verdict reached a log line and stopped
    there, so a rate-limited crawl kept requesting pages that would all
    be refused, and reported the empty result as a finished run.
    """
    from crawlme.digest.feed.base import PageProblem

    sched = _make_sched()
    sched._ctx.stats.reset()

    sched._note_not_content(PageProblem.UNAVAILABLE)
    assert sched._counters.refused_by == "", "a gone account is not a reason to stop"

    sched._note_not_content(PageProblem.BLOCKED)
    assert sched._counters.refused_by == "blocked"

    # Later refusals do not overwrite: the first one is what ended it.
    sched._note_not_content(PageProblem.LOGIN_REQUIRED)
    assert sched._counters.refused_by == "blocked"

    assert sched._ctx.stats.not_content == {"unavailable": 1, "blocked": 1, "login_required": 1}
    assert sched.summary()["not_content"] == {"unavailable": 1, "blocked": 1, "login_required": 1}


# shutdown ordering -------------------------------------------------------


@pytest.mark.asyncio
async def test_inflight_waits():
    """The pumps returning is not the run being over.

    A fetch is its own task with a page still to save and an analysis
    still to record. One run stopped with seven of them running, closed
    the storage and the analyzer underneath, and ended with seven pages
    fetched, saved, and never analysed -- with the analyzer's retries
    for them still arriving in the log after the crawl had reported
    itself complete.
    """
    sched = _make_sched()
    order: list[str] = []
    released = asyncio.Event()

    async def _slow_fetch():
        await released.wait()
        order.append("fetch")

    sched._storage.close = AsyncMock(side_effect=lambda: order.append("close"))
    task = asyncio.create_task(_slow_fetch())
    sched._inflight.add(task)
    task.add_done_callback(sched._inflight.discard)

    settling = asyncio.create_task(sched._settle_inflight())
    await asyncio.sleep(0)
    assert not settling.done(), "it must wait, not walk past"
    released.set()
    await settling
    await sched.aclose()
    assert order == ["fetch", "close"]


@pytest.mark.asyncio
async def test_inflight_gone(caplog, monkeypatch):
    """A backstop, not a promise: the process must still be able to exit."""
    sched = _make_sched()
    stuck = asyncio.create_task(asyncio.sleep(3600))
    sched._inflight.add(stuck)

    monkeypatch.setattr("crawlme.scheduler.engine._SETTLE_TIMEOUT", 0.05)
    with caplog.at_level(logging.WARNING):
        await sched._settle_inflight()

    assert stuck.cancelled() or stuck.done()
    assert "settle_timeout" in caplog.text


@pytest.mark.asyncio
async def test_missing_extra():
    """It is not about this page: every later page of the same format
    fails identically, so carrying on would spend the whole budget
    producing nothing and then report success."""
    from crawlme.digest.feed.base import FeedDependencyError

    sched = _make_sched()
    sched._goal = _goal(max_pages=5)
    sched._harvester = MagicMock(harvest=MagicMock(side_effect=FeedDependencyError("install crawl-me-maybe[rss]")))
    url = URL(raw="https://x.com/a", canonical="https://x.com/a", url_key="k1")
    sched._fetch_and_extract = AsyncMock(
        return_value=(MagicMock(item_id="i1", status_code=200, raw=b"x"), Page(url_key="k1", url=url))
    )
    sched._frontier.record_outcome = AsyncMock()
    sched._frontier.get_prefilter_context = MagicMock(return_value=MagicMock())
    sched._checkpoint = AsyncMock()

    await sched._handle_fetch(_item())

    assert "crawl-me-maybe[rss]" in sched._counters.fatal_error


@pytest.mark.asyncio
async def test_verdict_to_rank():
    """Ranking predicts, analysis establishes, and what analysis
    established goes back into the next prediction.

    The prompt has read `relevant_pages` all along; between v0.3.0 and
    this fix nothing filled it, so the section silently never appeared.
    """
    sched = _make_sched()
    sched._goal = _goal(max_pages=5)

    for i, cls in enumerate(("RELEVANT", "IRRELEVANT", "RELEVANT")):
        sched._on_analysis(
            AnalysisResult(
                page_id=f"p{i}",
                url_key=f"k{i}",
                classification=cls,
                relevance_score=0.9 if cls == "RELEVANT" else 0.1,
                feedback=AnalyzerFeedback(
                    classification=cls,
                    relevance_score=0.9 if cls == "RELEVANT" else 0.1,
                    url=f"https://example.com/{i}",
                    title=f"Post {i}",
                ),
            )
        )

    seen = [p["url"] for p in sched._relevant_pages]
    assert seen == ["https://example.com/0", "https://example.com/2"]

    captured: dict = {}

    async def _rank(goal, batch, history, page_contexts=None):
        captured["history"] = history
        return []

    sched._ranker = MagicMock(rank_batch=AsyncMock(side_effect=_rank))
    sched._frontier.push_batch = AsyncMock()
    url = URL(raw="https://x.com/c", canonical="https://x.com/c", url_key="ck")
    await sched._rank_and_enqueue([Candidate(url=url)])

    assert [p["title"] for p in captured["history"].relevant_pages] == ["Post 0", "Post 2"]


def test_seen_bounded():
    """Unbounded, it would grow for a whole run only to be sliced away
    at the prompt every time."""
    from crawlme.scheduler.engine import _SEEN_SO_FAR

    sched = _make_sched()
    for i in range(_SEEN_SO_FAR + 4):
        sched._on_analysis(
            AnalysisResult(
                page_id=f"p{i}",
                url_key=f"k{i}",
                classification="RELEVANT",
                relevance_score=0.9,
                feedback=AnalyzerFeedback(classification="RELEVANT", relevance_score=0.9, url=f"https://x/{i}"),
            )
        )

    assert len(sched._relevant_pages) == _SEEN_SO_FAR
    assert sched._relevant_pages[-1]["url"] == f"https://x/{_SEEN_SO_FAR + 3}"


@pytest.mark.asyncio
async def test_pause_settles():
    """Polling a counter works while the loop is healthy and does
    nothing while it is being torn down.  An interrupted run used to
    leave its fetches pending, print "Task was destroyed but it is
    pending", and leave its row saying RUNNING for ever."""
    finished: list[str] = []

    async def _slow() -> None:
        await asyncio.sleep(0.05)
        finished.append("done")

    sched = _make_sched()
    sched._task = _task()
    sched._checkpoint = AsyncMock()
    task = asyncio.create_task(_slow())
    sched._inflight.add(task)
    task.add_done_callback(sched._inflight.discard)

    await sched.pause()

    assert finished == ["done"], "pause returned before the fetch had finished"
    assert task.done()
    assert sched._state == "PAUSED"


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
    sched._goal = _goal(max_pages=100)
    return sched, frontier


@pytest.mark.asyncio
async def test_page_same_depth():
    """More of the same listing, not a hop away from it. Counting it
    would spend the depth budget on standing still."""
    sched, frontier = _paging_sched()
    item = FrontierItem(url=_url("https://www.reddit.com/r/x/"), url_key="k", depth=2)
    ctx = MagicMock()
    await sched._enqueue_next_page("https://www.reddit.com/r/x/?after=t3_a", item, ctx, "seed")
    pushed = frontier.push_batch.await_args.args[0]
    assert [p.depth for p in pushed] == [2]
    assert pushed[0].score_source == "listing_page"


@pytest.mark.asyncio
async def test_page_not_top():
    """More raw material is worth less than a post the ranker liked. At
    full priority a six-page budget went entirely on listings."""
    sched, frontier = _paging_sched()
    item = FrontierItem(url=_url("https://www.reddit.com/r/x/"), url_key="k", depth=0)
    await sched._enqueue_next_page("https://www.reddit.com/r/x/?after=t3_a", item, MagicMock(), "seed")
    assert frontier.push_batch.await_args.args[0][0].priority < 1.0


@pytest.mark.asyncio
async def test_pages_capped():
    """A subreddit pages indefinitely, and its pages arrive at a seed's
    own depth, so nothing else would stop them."""
    from crawlme.scheduler.engine import _MAX_LISTING_PAGES

    sched, frontier = _paging_sched()
    item = FrontierItem(url=_url("https://www.reddit.com/r/x/"), url_key="k", depth=0)
    ctx = MagicMock()
    for i in range(_MAX_LISTING_PAGES + 3):
        await sched._enqueue_next_page(f"https://www.reddit.com/r/x/?after=t3_{i}", item, ctx, "seed")
    assert frontier.push_batch.await_count == _MAX_LISTING_PAGES


@pytest.mark.asyncio
async def test_cap_per_listing():
    """Thirty accounts is thirty listings, not one budget between them."""
    from crawlme.scheduler.engine import _MAX_LISTING_PAGES

    sched, frontier = _paging_sched()
    ctx = MagicMock()
    for seed in ("a", "b"):
        item = FrontierItem(url=_url(f"https://www.reddit.com/r/{seed}/"), url_key=seed, depth=0)
        for i in range(_MAX_LISTING_PAGES):
            await sched._enqueue_next_page(f"https://www.reddit.com/r/{seed}/?after=t3_{i}", item, ctx, seed)
    assert frontier.push_batch.await_count == _MAX_LISTING_PAGES * 2


@pytest.mark.asyncio
async def test_page_drop_uncap():
    """Robots or scope can refuse it, and a refusal must not eat the
    allowance for pages that would have been allowed."""
    sched, frontier = _paging_sched()
    sched._prefilter.check.return_value = (MagicMock(value="drop"), "robots")
    item = FrontierItem(url=_url("https://www.reddit.com/r/x/"), url_key="k", depth=0)
    await sched._enqueue_next_page("https://www.reddit.com/r/x/?after=t3_a", item, MagicMock(), "seed")
    frontier.push_batch.assert_not_awaited()
    assert sched._pages_of_listing.get("seed", 0) == 0


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
    sched._goal = _goal(max_pages=5)
    return sched, storage, fetcher


@pytest.mark.asyncio
async def test_robots_blocks():
    """robots.txt was fetched by nothing and enforced on nothing: the
    cache table was empty on every run and allow_fetch always said yes."""
    sched, _, fetcher = _robots_sched("User-agent: *\nDisallow: /\n")
    item = FrontierItem(url=_url("https://x.com/page"), url_key="k", reg_domain="x.com")
    assert await sched._fetch_and_extract(item) is None
    # The only fetch was robots.txt itself.
    assert [c.args[0].url.canonical for c in fetcher.fetch.await_args_list] == ["https://x.com/robots.txt"]
    assert sched._ctx.stats.robots_blocked == 1


@pytest.mark.asyncio
async def test_robots_once():
    """Every page of a crawl on one host would otherwise pay for it."""
    sched, _, fetcher = _robots_sched("User-agent: *\nDisallow: /\n")
    for i in range(3):
        item = FrontierItem(url=_url(f"https://x.com/p{i}"), url_key=f"k{i}", reg_domain="x.com")
        await sched._fetch_and_extract(item)
    robots_calls = [c for c in fetcher.fetch.await_args_list if c.args[0].url.canonical.endswith("robots.txt")]
    assert len(robots_calls) == 1


@pytest.mark.asyncio
async def test_robots_cached():
    """It survives the run it was read in, which is the point of a TTL."""
    cached = {"raw": "User-agent: *\nDisallow: /\n", "fetched_at": _utcnow().isoformat(), "ttl": 86400}
    sched, _, fetcher = _robots_sched("User-agent: *\nDisallow:\n", cached=cached)
    item = FrontierItem(url=_url("https://x.com/page"), url_key="k", reg_domain="x.com")
    assert await sched._fetch_and_extract(item) is None
    fetcher.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_robots_absent():
    """A 404 states no policy, and a site briefly down must not have its
    whole domain closed off."""
    from crawlme.pioneer.robots import RobotsPolicy

    storage = MagicMock()
    storage.get_robots = AsyncMock(return_value=None)
    fetcher = MagicMock(aclose=AsyncMock())
    fetcher.fetch = AsyncMock(side_effect=OSError("connection refused"))
    sched = _make_sched(storage=storage, fetcher=fetcher, robots=RobotsPolicy(agent="a"))
    sched._goal = _goal(max_pages=5)
    await sched._ensure_robots("x.com")
    assert sched._robots.allow_fetch("https://x.com/anything")


@pytest.mark.asyncio
async def test_seeds_unenhanced_by_default():
    """Off unless asked for: no call, and the module is not even loaded."""
    sched = _make_sched(settings=Settings(enhance_seeds=False))
    assert await sched.enhance_seeds(_goal(), [MagicMock()]) == []


@pytest.mark.asyncio
async def test_no_seeds_nothing_to_enhance():
    """Nothing to widen, and the model would have no example to follow."""
    sched = _make_sched(settings=Settings(enhance_seeds=True))
    assert await sched.enhance_seeds(_goal(), []) == []


@pytest.mark.asyncio
async def test_enhanced_seeds_are_marked():
    """The buffer reads this to give them the smaller share."""
    from crawlme.schemas import URL, Candidate

    url = URL(raw="https://a.com/", canonical="https://a.com/", url_key="a", reg_domain="a.com")
    proposed = Candidate(url=url, seed_ext=True)
    sched = _make_sched(settings=Settings(enhance_seeds=True))
    with patch("crawlme.pioneer.seed_enhancer.enhance", AsyncMock(return_value=([proposed], 1, []))):
        got = await sched.enhance_seeds(_goal(), [MagicMock(url=url)])
    assert [c.seed_ext for c in got] == [True]


@pytest.mark.asyncio
async def test_a_proposed_seed_is_credited_for_what_it_found():
    """The report ranks them by this, and it is the whole reason to
    print them: a seed that earned its place is one to keep."""
    from crawlme.pioneer.canonicalizer import Canonicalizer
    from crawlme.schemas import AnalyzerFeedback

    canon = Canonicalizer()
    seed = "https://ig.test/acct/"
    seed_key = canon.canonicalize(seed, seed).url_key
    post = "https://ig.test/acct/p/1/"
    post_key = canon.canonicalize(post, post).url_key

    sched = _make_sched(canonicalizer=canon)
    sched._pages.open(post_key, post, seed_key)
    sched._proposed_seeds = {seed_key: (seed, "why")}
    sched._counters.relevance_threshold = 0.7

    sched._on_analysis(
        AnalysisResult(classification="RELEVANT", relevance_score=0.9, feedback=AnalyzerFeedback(url=post, title="t"))
    )
    why, tally = sched.summary()["proposed_seeds"][seed]
    assert (why, tally[0]) == ("why", 1)


@pytest.mark.asyncio
async def test_a_proposed_seed_that_found_nothing_says_so():
    sched = _make_sched()
    sched._proposed_seeds = {"k": ("https://ig.test/acct/", "why")}
    why, tally = sched.summary()["proposed_seeds"]["https://ig.test/acct/"]
    assert (why, tally[0]) == ("why", 0)


@pytest.mark.asyncio
async def test_seed_credited_mid_judge():
    """The sink runs during the analyze call, and it reads maps that used
    to be filled only after that call returned. Every verdict therefore
    landed with its own page still unknown, so a proposed seed reported
    finding nothing however much it found."""
    sched = _make_sched()
    sched._goal = _goal()
    sched._proposed_seeds = {"seed-k": ("https://ext.test/", "why")}
    sched._pages.open("k1", "https://ext.test/k1", "seed-k")

    url = URL(raw="https://ext.test/p/1", canonical="https://ext.test/p/1", url_key="pk", reg_domain="ext.test")
    page = Page(url_key="pk", url=url)
    result = MagicMock(item_id="i1", status_code=200, raw=b"x")

    async def _analyze(p, _g):
        sched._on_analysis(
            AnalysisResult(
                classification="RELEVANT",
                relevance_score=0.9,
                feedback=AnalyzerFeedback(url=p.url.canonical, title="t"),
            )
        )

    sched._analyzer = MagicMock(analyze=AsyncMock(side_effect=_analyze))
    sched._fetch_and_extract = AsyncMock(return_value=(result, page))
    sched._frontier.record_outcome = AsyncMock()
    sched._frontier.get_prefilter_context = MagicMock(return_value=MagicMock())
    sched._checkpoint = AsyncMock()

    await sched._handle_fetch(_item())

    why, tally = sched.summary()["proposed_seeds"]["https://ext.test/"]
    assert (why, tally[0]) == ("why", 1)


def _judge(sched, url_key, score):
    sched._on_analysis(
        AnalysisResult(
            url_key=url_key,
            classification="RELEVANT" if score >= 0.7 else "IRRELEVANT",
            relevance_score=score,
            feedback=AnalyzerFeedback(url=f"https://x.test/{url_key}", title="t"),
        )
    )


def test_a_listing_does_not_vote():
    """It is read for its links and can never be an answer. Counting it
    asks a page that was never in the running whether the run is
    working, and one run stopped for diminishing returns because five
    seeds put seven certain misses into a window of twenty."""
    sched = _make_sched()
    _judge(sched, "k1", 0.0)
    sched._pages.of("k1").listing = True
    sched._cast_relevance_vote("k1")
    assert list(sched._tally_by_seed[""].window) == []


def test_a_page_votes_once_judged():
    sched = _make_sched()
    _judge(sched, "k1", 0.9)
    sched._pages.of("k1").listing = False
    sched._cast_relevance_vote("k1")
    assert list(sched._tally_by_seed[""].window) == [True]


def test_a_late_verdict_still_votes():
    """A retried analysis lands long after link extraction, so the
    harvester's half is already in when the verdict arrives."""
    sched = _make_sched()
    sched._pages.of("k1").listing = False
    sched._cast_relevance_vote("k1")
    assert list(sched._tally_by_seed[""].window) == []
    _judge(sched, "k1", 0.9)
    assert list(sched._tally_by_seed[""].window) == [True]


def test_a_vote_is_cast_once():
    sched = _make_sched()
    _judge(sched, "k1", 0.9)
    sched._pages.of("k1").listing = False
    sched._cast_relevance_vote("k1")
    sched._cast_relevance_vote("k1")
    assert list(sched._tally_by_seed[""].window) == [True]


def test_the_tally_counts_listings():
    """Abstaining from the window is not abstaining from the run: a
    listing that somehow answers the goal is still an answer found."""
    sched = _make_sched()
    _judge(sched, "k1", 0.9)
    sched._pages.of("k1").listing = True
    sched._cast_relevance_vote("k1")
    assert sched._counters.relevant_found == 1


@pytest.mark.asyncio
async def test_backpressure_caps_inflight():
    """The fetch slot is released before the analysis, so tasks waiting
    for an LLM slot hold nothing back. One run reached forty-six parked
    on two analysis slots and abandoned thirty-three when it stopped."""
    sched = _make_sched(settings=Settings(fetch_concurrency=6, llm_concurrency=2))
    sched._task = _task()
    sched._counters.in_flight = 6 + 2 * 2
    sched._frontier.pop_next = AsyncMock(return_value=_item())

    sched._state = "RUNNING"
    task = asyncio.create_task(sched._fetch_pump())
    await asyncio.sleep(0.05)
    sched._state = "STOPPING"
    await asyncio.gather(task, return_exceptions=True)

    sched._frontier.pop_next.assert_not_awaited()


def test_enough_found_reads_the_target():
    sched = _make_sched()
    sched._counters.max_relevant = 15
    sched._counters.relevant_found = 14
    assert sched._enough_found() is False
    sched._counters.relevant_found = 15
    assert sched._enough_found() is True


def test_no_target_never_enough():
    """Zero means the run was given no target, not a target of zero."""
    sched = _make_sched()
    sched._counters.max_relevant = 0
    sched._counters.relevant_found = 99
    assert sched._enough_found() is False


@pytest.mark.asyncio
async def test_a_met_target_stops_analysis():
    """The stop only stops dispatch. Every task already out keeps going
    and each one that lands adds to the tally, so one run asked for
    fifteen and reported twenty-four."""
    sched = _make_sched()
    sched._goal = _goal()
    sched._counters.max_relevant = 15
    sched._counters.relevant_found = 15
    sched._analyzer = MagicMock(analyze=AsyncMock())
    url = URL(raw="https://x.test/p", canonical="https://x.test/p", url_key="pk", reg_domain="x.test")
    page = Page(url_key="pk", url=url)
    result = MagicMock(item_id="i", status_code=200, raw=b"x")
    sched._fetch_and_extract = AsyncMock(return_value=(result, page))
    sched._frontier.record_outcome = AsyncMock()
    sched._frontier.get_prefilter_context = MagicMock(return_value=MagicMock())
    sched._checkpoint = AsyncMock()

    await sched._handle_fetch(_item())

    sched._analyzer.analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_target_holds_under_a_queue():
    """The wait for an analysis slot used to happen inside the LLM
    client, past every check the scheduler could make, so a target met
    while forty-six pages were queued still had all forty-six analysed.
    Queued here instead, the check sits at the head of the queue and
    only what is already calling can overshoot."""
    sched = _make_sched(settings=Settings(llm_concurrency=2))
    sched._goal = _goal()
    sched._counters.max_relevant = 3
    sched._frontier.record_outcome = AsyncMock()
    sched._frontier.get_prefilter_context = MagicMock(return_value=MagicMock())
    sched._checkpoint = AsyncMock()

    calls = 0

    async def _analyze(_page, _goal_arg):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        sched._counters.relevant_found += 1  # worst case: every page counts

    sched._analyzer = MagicMock(analyze=_analyze)

    def _fetched(i):
        url = URL(raw=f"https://x.test/{i}", canonical=f"https://x.test/{i}", url_key=f"k{i}", reg_domain="x.test")
        return MagicMock(item_id="i", status_code=200, raw=b"x"), Page(url_key=f"k{i}", url=url)

    pages = [_fetched(i) for i in range(20)]
    sched._fetch_and_extract = AsyncMock(side_effect=pages)
    await asyncio.gather(*[sched._handle_fetch(_item()) for _ in range(20)])

    assert calls <= sched._counters.max_relevant + 2


def _vote(sched, seed, url_key, relevant):
    sched._pages.open(url_key, f"https://x.test/{url_key}", seed)
    sched._pages.of(url_key).relevant = relevant
    sched._pages.of(url_key).listing = False
    sched._cast_relevance_vote(url_key)


def test_a_cold_source_retires():
    """A full window of its own content with almost nothing to show."""
    sched = _make_sched()
    for i in range(RELEVANCE_WINDOW):
        _vote(sched, "seedA", f"k{i}", False)
    sched._frontier.retire.assert_called_once_with("seedA")
    assert sched._tally_by_seed["seedA"].retired


def test_one_cold_source_leaves_the_others():
    """The whole reason this is per seed. Read globally, one quiet shop's
    back catalogue ended a run with three sources still producing."""
    sched = _make_sched()
    for i in range(RELEVANCE_WINDOW):
        _vote(sched, "cold", f"c{i}", False)
        _vote(sched, "hot", f"h{i}", i % 3 == 0)
    assert sched._tally_by_seed["cold"].retired
    assert not sched._tally_by_seed["hot"].retired


def test_a_source_past_the_window_retires():
    """A feed is time-ordered per account, so reading past --since means
    that account is walked out. It says nothing about the others."""
    sched = _make_sched()
    sched._counters.since = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
    old = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    for i in range(MAX_STALE_STREAK):
        sched._note_page_age(_page_published(old), "seedA")
    assert sched._tally_by_seed["seedA"].retired
    sched._frontier.retire.assert_called_once_with("seedA")


def test_one_hit_resets_the_stale_streak():
    sched = _make_sched()
    sched._counters.since = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
    for at in (datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),) * 4:
        sched._note_page_age(_page_published(at), "seedA")
    sched._note_page_age(_page_published(datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)), "seedA")
    assert sched._tally_by_seed["seedA"].stale == 0


def test_recall_retires_nothing():
    """Reading the tail is the point of the mode, not evidence a source
    is done."""
    sched = _make_sched()
    sched._counters.recall = True
    for i in range(RELEVANCE_WINDOW):
        _vote(sched, "seedA", f"k{i}", False)
    assert not sched._tally_by_seed["seedA"].retired
    sched._frontier.retire.assert_not_called()


def test_a_source_retires_once():
    sched = _make_sched()
    for i in range(RELEVANCE_WINDOW * 2):
        _vote(sched, "seedA", f"k{i}", False)
    sched._frontier.retire.assert_called_once_with("seedA")


def test_an_unfiled_page_cannot_retire_anything():
    """The streak used to be looked up by a key the page did not have
    yet, so it always landed under the empty seed -- where retirement
    ignores it. Five sources shared one streak, each other's fresh pages
    reset it, and a --since of one month came back full of year-old
    posts."""
    sched = _make_sched()
    sched._counters.since = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
    old = _page_published(datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc))
    for _ in range(MAX_STALE_STREAK * 2):
        sched._note_page_age(old, "")
    assert not sched._tally_by_seed[""].retired
    sched._frontier.retire.assert_not_called()


def test_a_dead_pump_ends_the_run():
    """Both pumps are gathered with return_exceptions, so one that died
    left its exception in a list nobody read: a rank pump that lost its
    provider stopped scoring and the report said the run completed."""
    from crawlme.llm import LLMError

    sched = _make_sched()
    sched._note_pump_failures([None, LLMError("provider rejected the request")])
    assert sched._counters.fatal_error == "provider rejected the request"


def test_a_cancelled_pump_is_not_a_failure():
    """Stopping and pausing both cancel them on purpose."""
    sched = _make_sched()
    sched._note_pump_failures([asyncio.CancelledError(), None])
    assert not sched._counters.fatal_error


def test_the_first_failure_is_the_one_reported():
    sched = _make_sched()
    sched._note_pump_failures([RuntimeError("first"), RuntimeError("second")])
    assert sched._counters.fatal_error == "first"
