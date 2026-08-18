from __future__ import annotations

import asyncio
import datetime
import time

import pytest

from crawlme.pioneer.buffer import InMemoryBuffer
from crawlme.pioneer.frontier import PriorityFrontier
from crawlme.scheduler.stop_conds import check_stop
from crawlme.schemas import URL, CrawlTask, FrontierItem
from crawlme.state.context import CrawlCounters


def _task(state: str = "RUNNING") -> CrawlTask:
    return CrawlTask(task_id="t1", state=state)  # type: ignore[arg-type]


def _frontier(size: int = 0) -> PriorityFrontier:
    """Populate through the public API rather than the heap internals.

    Reaching into _heap/_items coupled these tests to one ordering
    implementation, which is exactly what the WorkSource seam removes.
    """
    f = PriorityFrontier()
    items = [
        FrontierItem(
            url=URL(raw=f"https://x.com/{i}", canonical=f"https://x.com/{i}", url_key=f"k{i}"),
            url_key=f"k{i}",
            priority=0.5,
        )
        for i in range(size)
    ]
    if items:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(f.push_batch(items))
    return f


def _buffer() -> InMemoryBuffer:
    return InMemoryBuffer()


def _counters(**kw) -> CrawlCounters:
    window = kw.pop("relevance_window", None)
    c = CrawlCounters(**kw)
    if window is not None:
        c.relevance_window.extend(window)
    return c


def _codes(reasons) -> list[str]:
    return [r.code for r in reasons]


# -- budget --------------------------------------------------------------


def test_budget_pages():
    reasons = check_stop(_task(), _frontier(), _buffer(), _counters(max_pages=10, pages_fetched=10))
    assert any(r.code == "BUDGET_PAGES" for r in reasons)


def test_budget_pages_not_reached():
    reasons = check_stop(_task(), _frontier(), _buffer(), _counters(max_pages=10, pages_fetched=5))
    assert not any(r.code == "BUDGET_PAGES" for r in reasons)


def test_budget_pages_unlimited():
    reasons = check_stop(_task(), _frontier(), _buffer(), _counters(max_pages=0, pages_fetched=999))
    assert not any(r.code == "BUDGET_PAGES" for r in reasons)


def test_budget_tokens():
    reasons = check_stop(_task(), _frontier(), _buffer(), _counters(max_tokens=5000, tokens_used=5000))
    assert any(r.code == "BUDGET_TOKENS" for r in reasons)


def test_budget_time():
    reasons = check_stop(
        _task(),
        _frontier(),
        _buffer(),
        _counters(max_duration_sec=1, started_at=time.monotonic() - 10),
    )
    assert any(r.code == "BUDGET_TIME" for r in reasons)


def test_budget_time_not_reached():
    reasons = check_stop(
        _task(),
        _frontier(),
        _buffer(),
        _counters(max_duration_sec=3600, started_at=time.monotonic()),
    )
    assert not any(r.code == "BUDGET_TIME" for r in reasons)


# -- frontier drained ----------------------------------------------------


def test_frontier_drained():
    reasons = check_stop(
        _task(),
        _frontier(size=0),
        _buffer(),
        _counters(in_flight=0),
    )
    assert any(r.code == "FRONTIER_DRAINED" for r in reasons)


def test_frontier_not_drained_with_items():
    reasons = check_stop(
        _task(),
        _frontier(size=3),
        _buffer(),
        _counters(in_flight=0),
    )
    assert not any(r.code == "FRONTIER_DRAINED" for r in reasons)


def test_frontier_not_drained_with_in_flight():
    reasons = check_stop(
        _task(),
        _frontier(size=0),
        _buffer(),
        _counters(in_flight=2),
    )
    assert not any(r.code == "FRONTIER_DRAINED" for r in reasons)


@pytest.mark.asyncio
async def test_frontier_not_drained_with_buffered():
    buf = _buffer()
    await buf.add([_candidate()])
    reasons = check_stop(_task(), _frontier(size=0), buf, _counters(in_flight=0))
    assert not any(r.code == "FRONTIER_DRAINED" for r in reasons)


def test_frontier_not_drained_while_a_batch_is_being_ranked():
    """A batch inside a rank call is in no container the check can see.

    It has left the buffer and has not reached the frontier, and a rank
    call is a network round trip, so the window is seconds wide. A real
    run ended here reporting COMPLETED after fetching one page: the feed
    had handed its whole yield to one rank batch, so the frontier was
    legitimately empty for the whole call.
    """
    reasons = check_stop(
        _task(),
        _frontier(size=0),
        _buffer(),
        _counters(in_flight=0, ranking_in_flight=11),
    )
    assert not any(r.code == "FRONTIER_DRAINED" for r in reasons)


# -- diminishing returns -------------------------------------------------


def test_diminishing_returns():
    # Last 20 pages: only 1 relevant.
    window = [False] * 19 + [True]
    reasons = check_stop(_task(), _frontier(), _buffer(), _counters(relevance_window=window))
    assert any(r.code == "DIMINISHING_RETURNS" for r in reasons)


def test_diminishing_returns_not_enough_pages():
    window = [False] * 10
    reasons = check_stop(_task(), _frontier(), _buffer(), _counters(relevance_window=window))
    assert not any(r.code == "DIMINISHING_RETURNS" for r in reasons)


def test_diminishing_returns_still_finding():
    window = [True] * 5 + [False] * 15
    reasons = check_stop(_task(), _frontier(), _buffer(), _counters(relevance_window=window))
    assert not any(r.code == "DIMINISHING_RETURNS" for r in reasons)


# -- user requested ------------------------------------------------------


def _since_counters(**kw: object) -> CrawlCounters:
    base = {"since": datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc), "seed_count": 1}
    base.update(kw)
    return CrawlCounters(**base)  # type: ignore[arg-type]


def test_time_horizon_dormant_without_since():
    """Every run that does not ask for a window must be unaffected."""
    c = CrawlCounters(stale_streak=99)
    assert "TIME_HORIZON" not in _codes(check_stop(_task(), _frontier(), _buffer(), c))


def test_time_horizon_fires_on_streak():
    c = _since_counters(stale_streak=5)
    assert "TIME_HORIZON" in _codes(check_stop(_task(), _frontier(), _buffer(), c))


def test_time_horizon_holds_below_streak():
    c = _since_counters(stale_streak=4)
    assert "TIME_HORIZON" not in _codes(check_stop(_task(), _frontier(), _buffer(), c))


def test_time_horizon_disabled_by_zero_threshold():
    c = _since_counters(stale_streak=50, max_stale_streak=0)
    assert "TIME_HORIZON" not in _codes(check_stop(_task(), _frontier(), _buffer(), c))


def test_time_horizon_dormant_when_a_run_has_several_entry_points():
    """Thirty shops interleave thirty traversals, so a streak spans accounts.

    One quiet shop's back catalogue must not end the run before an active
    shop is ever reached.
    """
    c = _since_counters(stale_streak=99, seed_count=30)
    assert "TIME_HORIZON" not in _codes(check_stop(_task(), _frontier(), _buffer(), c))


def test_time_horizon_dormant_when_the_entry_points_are_unknown():
    """Dormant is the safe direction: overspending beats missing results."""
    c = _since_counters(stale_streak=99, seed_count=0)
    assert "TIME_HORIZON" not in _codes(check_stop(_task(), _frontier(), _buffer(), c))


def test_user_requested():
    reasons = check_stop(_task(state="STOPPING"), _frontier(), _buffer(), _counters())
    assert any(r.code == "USER_REQUESTED" for r in reasons)


def test_user_not_requested_when_running():
    reasons = check_stop(_task(state="RUNNING"), _frontier(), _buffer(), _counters())
    assert not any(r.code == "USER_REQUESTED" for r in reasons)


# -- fatal ---------------------------------------------------------------


def test_fatal():
    reasons = check_stop(_task(), _frontier(), _buffer(), _counters(fatal_error="disk full"))
    assert any(r.code == "FATAL" for r in reasons)


# -- multi ---------------------------------------------------------------


def test_multiple_reasons():
    reasons = check_stop(
        _task(state="STOPPING"),
        _frontier(),
        _buffer(),
        _counters(max_pages=10, pages_fetched=10, fatal_error="disk full"),
    )
    codes = {r.code for r in reasons}
    assert "BUDGET_PAGES" in codes
    assert "USER_REQUESTED" in codes
    assert "FATAL" in codes


def test_no_reasons_when_everything_fine():
    reasons = check_stop(
        _task(state="RUNNING"),
        _frontier(size=5),
        _buffer(),
        _counters(
            max_pages=50,
            pages_fetched=10,
            max_tokens=100000,
            tokens_used=5000,
            max_duration_sec=3600,
            started_at=time.monotonic(),
            in_flight=2,
            relevance_window=[True, False],
        ),
    )
    assert reasons == []


def _candidate():
    from crawlme.schemas import URL, Candidate

    return Candidate(
        url=URL(raw="https://x.com", canonical="https://x.com", url_key="k1"),
    )


def test_relevance_window_only_keeps_the_recent_slice():
    """A run longer than the window must not accumulate forever."""
    c = CrawlCounters()
    for _ in range(100):
        c.relevance_window.append(False)
    assert len(c.relevance_window) == 20


def test_diminishing_returns_forgets_an_old_dry_spell():
    """Twenty misses then twenty hits is a healthy crawl, not a dead one."""
    c = _counters(relevance_window=[False] * 20 + [True] * 20)
    assert not any(r.code == "DIMINISHING_RETURNS" for r in check_stop(_task(), _frontier(), _buffer(), c))
