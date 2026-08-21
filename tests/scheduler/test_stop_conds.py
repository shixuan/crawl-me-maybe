from __future__ import annotations

import asyncio
import datetime
import time

import pytest

from crawlme.pioneer.frontier import GatedFrontier
from crawlme.scheduler.stop_conds import check_stop
from crawlme.schemas import URL, Candidate, CrawlTask, FrontierItem
from crawlme.state.context import CrawlCounters


def _task(state: str = "RUNNING") -> CrawlTask:
    return CrawlTask(task_id="t1", state=state)  # type: ignore[arg-type]


def _frontier(size: int = 0, scoring: int = 0, waiting: int = 0) -> GatedFrontier:
    """Populate through the public API rather than the heap internals.

    Reaching into _heap/_items coupled these tests to one ordering
    implementation, which is exactly what the Ordering seam removes.
    """
    f = GatedFrontier()
    items = [
        FrontierItem(
            url=URL(raw=f"https://x.com/{i}", canonical=f"https://x.com/{i}", url_key=f"k{i}"),
            url_key=f"k{i}",
            priority=0.5,
        )
        for i in range(size)
    ]
    loop = asyncio.get_event_loop_policy().new_event_loop()
    if items:
        loop.run_until_complete(f.push_batch(items))
    if scoring or waiting:
        n = scoring + waiting
        loop.run_until_complete(f.push_candidates([_waiting_candidate(i) for i in range(n)]))
        if scoring:
            loop.run_until_complete(f.take_for_ranking(scoring))
    return f


def _waiting_candidate(i: int) -> Candidate:
    return Candidate(
        url=URL(raw=f"https://x.com/c{i}", canonical=f"https://x.com/c{i}", url_key=f"c{i}"),
        depth=1,
    )


def _counters(**kw) -> CrawlCounters:
    window = kw.pop("relevance_window", None)
    c = CrawlCounters(**kw)
    if window is not None:
        c.relevance_window.extend(window)
    return c


def _codes(reasons) -> list[str]:
    return [r.code for r in reasons]


# -- budget --------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "fires"),
    [
        ({"max_pages": 10, "pages_fetched": 10}, True),
        ({"max_pages": 10, "pages_fetched": 5}, False),
        # Zero is how a run says "no ceiling", not a ceiling of zero.
        ({"max_pages": 0, "pages_fetched": 999}, False),
    ],
)
def test_budget_pages(kw, fires):
    assert ("BUDGET_PAGES" in _codes(check_stop(_task(), _frontier(), _counters(**kw)))) is fires


@pytest.mark.parametrize(
    ("kw", "fires"),
    [
        ({"max_tokens": 5000, "tokens_used": 5000}, True),
        ({"max_tokens": 5000, "tokens_used": 4999}, False),
        ({"max_tokens": 0, "tokens_used": 999999}, False),
    ],
)
def test_budget_tokens(kw, fires):
    assert ("BUDGET_TOKENS" in _codes(check_stop(_task(), _frontier(), _counters(**kw)))) is fires


@pytest.mark.parametrize(
    ("max_duration_sec", "elapsed", "fires"),
    [(1, 10, True), (3600, 0, False)],
)
def test_budget_time(max_duration_sec, elapsed, fires):
    c = _counters(max_duration_sec=max_duration_sec, started_at=time.monotonic() - elapsed)
    assert ("BUDGET_TIME" in _codes(check_stop(_task(), _frontier(), c))) is fires


# -- frontier drained ----------------------------------------------------


@pytest.mark.parametrize(
    ("frontier_kw", "in_flight", "fires"),
    [
        ({"size": 0}, 0, True),
        ({"size": 3}, 0, False),
        ({"size": 0}, 2, False),
        # Unscored candidates are still work.  The check used to be handed
        # the waiting half separately; a frontier that owns both answers
        # on its own, so there is one place that knows what is left.
        ({"size": 0, "waiting": 1}, 0, False),
        # A batch inside a rank call is in neither half, and a rank call
        # is a network round trip.  A real run reported COMPLETED after
        # one page: the feed had handed its whole yield to one batch.
        ({"size": 0, "scoring": 11}, 0, False),
    ],
)
def test_frontier_drained(frontier_kw, in_flight, fires):
    reasons = check_stop(_task(), _frontier(**frontier_kw), _counters(in_flight=in_flight))
    assert ("FRONTIER_DRAINED" in _codes(reasons)) is fires


@pytest.mark.parametrize("blocked", [7, 0])
def test_ceiling_is_named_alongside_drained(blocked):
    """Both facts, because either one alone misreports the run.

    A feed run ended at fifty pages with a hundred and sixty candidates
    still waiting and reported only "completed": every one of them was
    refused by a per-domain ceiling that, on one platform, is a total.
    Reporting the ceiling *instead* then hid the opposite case, where a
    graph crawl refuses thousands along the way and still finishes.
    """
    frontier = _frontier(size=0)
    frontier.blocked_by_domain_budget = blocked
    codes = _codes(check_stop(_task(), frontier, _counters(in_flight=0)))
    assert "FRONTIER_DRAINED" in codes
    assert ("DOMAIN_BUDGET" in codes) is bool(blocked)


# -- diminishing returns -------------------------------------------------


@pytest.mark.parametrize(
    ("window", "fires"),
    [
        ([False] * 19 + [True], True),
        ([False] * 10, False),  # too few pages to conclude anything
        ([True] * 5 + [False] * 15, False),
        # Twenty misses then twenty hits is a healthy crawl, not a dead
        # one: the window has to forget the old dry spell.
        ([False] * 20 + [True] * 20, False),
    ],
)
def test_diminishing_returns(window, fires):
    c = _counters(relevance_window=window)
    assert ("DIMINISHING_RETURNS" in _codes(check_stop(_task(), _frontier(), c))) is fires


def test_relevance_window_keeps_only_the_recent_slice():
    """A run longer than the window must not accumulate forever."""
    c = CrawlCounters()
    for _ in range(100):
        c.relevance_window.append(False)
    assert len(c.relevance_window) == 20


# -- time horizon --------------------------------------------------------


def _since_counters(**kw: object) -> CrawlCounters:
    base = {"since": datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc), "seed_count": 1}
    base.update(kw)
    return CrawlCounters(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kw", "fires"),
    [
        ({"stale_streak": 5}, True),
        ({"stale_streak": 4}, False),
        ({"stale_streak": 50, "max_stale_streak": 0}, False),
        # Thirty shops interleave thirty traversals, so a streak spans
        # accounts: one quiet shop's back catalogue must not end the run
        # before an active shop is ever reached.
        ({"stale_streak": 99, "seed_count": 30}, False),
        # Unknown entry points stay dormant.  Overspending beats missing.
        ({"stale_streak": 99, "seed_count": 0}, False),
    ],
)
def test_time_horizon(kw, fires):
    assert ("TIME_HORIZON" in _codes(check_stop(_task(), _frontier(), _since_counters(**kw)))) is fires


def test_time_horizon_dormant_without_since():
    """Every run that does not ask for a window must be unaffected."""
    c = CrawlCounters(stale_streak=99)
    assert "TIME_HORIZON" not in _codes(check_stop(_task(), _frontier(), c))


# -- what the run is for -------------------------------------------------


@pytest.mark.parametrize(
    ("max_relevant", "found", "fires"),
    [
        (50, 50, True),
        (50, 49, False),
        (0, 500, False),  # no target: the budgets decide, as they always did
        (10, 13, True),  # analysis lags fetching, so the tally can overshoot
    ],
)
def test_max_relevant(max_relevant, found, fires):
    """The only stop condition that states a goal rather than a ceiling.

    Without it a page budget has to stand in for one, and "sixty pages"
    tells nobody how many answers that buys: one run spent sixty and
    returned twenty-two.
    """
    c = _counters(max_relevant=max_relevant)
    c.relevant_found = found
    assert ("MAX_RELEVANT" in _codes(check_stop(_task(), _frontier(size=9), c))) is fires


# -- the platform refusing the crawl -------------------------------------


@pytest.mark.parametrize(
    ("refused_by", "code"),
    [
        ("blocked", "RATE_LIMITED"),
        ("login_required", "LOGIN_REQUIRED"),
        # A gone account is about that account, so it never arrives here:
        # losing the other twenty-nine over it is the failure the split
        # exists to prevent.
        ("", None),
    ],
)
def test_platform_refused(refused_by, code):
    """One refusal is enough: the rest of the run would be refused too.

    A rate-limited crawl used to read as a quiet week. Every listing came
    back with no posts, the frontier drained on schedule, and the run
    reported completion having learned nothing.
    """
    codes = _codes(check_stop(_task(), _frontier(), _counters(refused_by=refused_by)))
    refusals = [c for c in codes if c in ("RATE_LIMITED", "LOGIN_REQUIRED")]
    assert refusals == ([code] if code else [])


# -- user, fatal, and everything at once ---------------------------------


@pytest.mark.parametrize(("state", "fires"), [("STOPPING", True), ("RUNNING", False)])
def test_user_requested(state, fires):
    assert ("USER_REQUESTED" in _codes(check_stop(_task(state=state), _frontier(), _counters()))) is fires


def test_fatal():
    assert "FATAL" in _codes(check_stop(_task(), _frontier(), _counters(fatal_error="disk full")))


def test_multiple_reasons():
    """Every check runs; the run reports all of them, not the first."""
    codes = set(
        _codes(
            check_stop(
                _task(state="STOPPING"),
                _frontier(),
                _counters(max_pages=10, pages_fetched=10, fatal_error="disk full"),
            )
        )
    )
    assert {"BUDGET_PAGES", "USER_REQUESTED", "FATAL"} <= codes


def test_no_reasons_when_healthy():
    reasons = check_stop(
        _task(state="RUNNING"),
        _frontier(size=5),
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


# -- an adapter that stopped recognising its platform --------------------


@pytest.mark.parametrize(
    ("seen", "empty", "fires"),
    [
        (5, 5, True),
        # One or two quiet accounts is a normal week, not a redesign.
        (2, 2, False),
        # Some worked, so the adapter still understands the markup.
        (5, 4, False),
        (5, 0, False),
        (0, 0, False),
    ],
)
def test_adapter_empty(seen, empty, fires):
    """Readable listings that hold nothing is what a redesign looks like.

    The pages still arrive and the adapter still recognises them as
    pages; it recognises nothing on any of them. The run then drains on
    schedule and reports a finished crawl of a silent platform.
    """
    c = _counters(in_flight=0, listings_seen=seen, listings_empty=empty)
    assert ("ADAPTER_EMPTY" in _codes(check_stop(_task(), _frontier(), c))) is fires


def test_adapter_empty_waits_for_the_end():
    """Mid-run there is no telling a dead adapter from a slow start."""
    c = _counters(in_flight=2, listings_seen=5, listings_empty=5)
    assert "ADAPTER_EMPTY" not in _codes(check_stop(_task(), _frontier(size=3), c))
