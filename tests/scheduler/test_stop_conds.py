from __future__ import annotations

import asyncio
import time

import pytest

from crawlme.pioneer.frontier import GatedFrontier
from crawlme.scheduler.stop_conds import check_stop, why_retire
from crawlme.schemas import URL, Candidate, CrawlTask, FrontierItem
from crawlme.state.context import Limits, Progress


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


def _split(**kw) -> tuple[Limits, Progress]:
    """Route each field to the half that owns it."""
    lim = {k: v for k, v in kw.items() if k in _LIMIT_FIELDS}
    return Limits(**lim), Progress(**{k: v for k, v in kw.items() if k not in _LIMIT_FIELDS})


_LIMIT_FIELDS = {
    "max_pages",
    "max_tokens",
    "max_duration_sec",
    "max_relevant",
    "relevance_threshold",
    "recall",
    "since",
}


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
    assert ("BUDGET_PAGES" in _codes(check_stop(_task(), _frontier(), *_split(**kw)))) is fires


@pytest.mark.parametrize(
    ("kw", "fires"),
    [
        ({"max_tokens": 5000, "tokens_used": 5000}, True),
        ({"max_tokens": 5000, "tokens_used": 4999}, False),
        ({"max_tokens": 0, "tokens_used": 999999}, False),
    ],
)
def test_budget_tokens(kw, fires):
    assert ("BUDGET_TOKENS" in _codes(check_stop(_task(), _frontier(), *_split(**kw)))) is fires


@pytest.mark.parametrize(
    ("max_duration_sec", "elapsed", "fires"),
    [(1, 10, True), (3600, 0, False)],
)
def test_budget_time(max_duration_sec, elapsed, fires):
    lim, prog = _split(max_duration_sec=max_duration_sec, started_at=time.monotonic() - elapsed)
    assert ("BUDGET_TIME" in _codes(check_stop(_task(), _frontier(), lim, prog))) is fires


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
def test_drained(frontier_kw, in_flight, fires):
    reasons = check_stop(_task(), _frontier(**frontier_kw), *_split(in_flight=in_flight))
    assert ("FRONTIER_DRAINED" in _codes(reasons)) is fires


@pytest.mark.parametrize("blocked", [7, 0])
def test_ceiling_named(blocked):
    """Both facts, because either one alone misreports the run.

    A feed run ended at fifty pages with a hundred and sixty candidates
    still waiting and reported only "completed": every one of them was
    refused by a per-domain ceiling that, on one platform, is a total.
    Reporting the ceiling *instead* then hid the opposite case, where a
    graph crawl refuses thousands along the way and still finishes.
    """
    frontier = _frontier(size=0)
    frontier.blocked_by_domain_budget = blocked
    codes = _codes(check_stop(_task(), frontier, *_split(in_flight=0)))
    assert "FRONTIER_DRAINED" in codes
    assert ("DOMAIN_BUDGET" in codes) is bool(blocked)


# -- diminishing returns -------------------------------------------------


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
    lim, prog = _split(max_relevant=max_relevant, relevant_found=found)
    assert ("MAX_RELEVANT" in _codes(check_stop(_task(), _frontier(size=9), lim, prog))) is fires


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
def test_refused(refused_by, code):
    """One refusal is enough: the rest of the run would be refused too.

    A rate-limited crawl used to read as a quiet week. Every listing came
    back with no posts, the frontier drained on schedule, and the run
    reported completion having learned nothing.
    """
    codes = _codes(check_stop(_task(), _frontier(), *_split(refused_by=refused_by)))
    refusals = [c for c in codes if c in ("RATE_LIMITED", "LOGIN_REQUIRED")]
    assert refusals == ([code] if code else [])


# -- user, fatal, and everything at once ---------------------------------


@pytest.mark.parametrize(("state", "fires"), [("STOPPING", True), ("RUNNING", False)])
def test_user_requested(state, fires):
    assert ("USER_REQUESTED" in _codes(check_stop(_task(state=state), _frontier(), *_split()))) is fires


def test_fatal():
    assert "FATAL" in _codes(check_stop(_task(), _frontier(), *_split(fatal_error="disk full")))


def test_many_reasons():
    """Every check runs; the run reports all of them, not the first."""
    codes = set(
        _codes(
            check_stop(
                _task(state="STOPPING"),
                _frontier(),
                *_split(max_pages=10, pages_fetched=10, fatal_error="disk full"),
            )
        )
    )
    assert {"BUDGET_PAGES", "USER_REQUESTED", "FATAL"} <= codes


def test_healthy_quiet():
    reasons = check_stop(
        _task(state="RUNNING"),
        _frontier(size=5),
        *_split(
            max_pages=50,
            pages_fetched=10,
            max_tokens=100000,
            tokens_used=5000,
            max_duration_sec=3600,
            started_at=time.monotonic(),
            in_flight=2,
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
    lim, prog = _split(in_flight=0, listings_seen=seen, listings_empty=empty)
    assert ("ADAPTER_EMPTY" in _codes(check_stop(_task(), _frontier(), lim, prog))) is fires


def test_empty_waits():
    """Mid-run there is no telling a dead adapter from a slow start."""
    lim, prog = _split(in_flight=2, listings_seen=5, listings_empty=5)
    assert "ADAPTER_EMPTY" not in _codes(check_stop(_task(), _frontier(size=3), lim, prog))


# -- one source ----------------------------------------------------------


@pytest.mark.parametrize(
    ("window", "stale", "why"),
    [
        ([False] * 20, 0, "nothing in its last 20 pages"),
        ([True] * 2 + [False] * 18, 0, None),  # two hits is enough to keep it
        ([False] * 19, 0, None),  # a window that is not full yet says nothing
        ([], 5, "5 pages in a row older than the window"),
        ([], 4, None),
    ],
)
def test_why_retire(window, stale, why):
    assert why_retire(window, stale) == why


def test_a_full_window_of_hits_keeps_it():
    assert why_retire([True] * 20, 0) is None


def test_a_stop_condition_cannot_see_the_ledger():
    """The split that keeps statistics out of the stopping criteria is
    held by this signature, not by care. A number nothing stops on used
    to sit among the ones that do, and adding another was one keyword
    away."""
    import inspect

    from crawlme.state.context import Ledger

    params = inspect.signature(check_stop).parameters
    assert "ledger" not in params
    assert not any(p.annotation is Ledger for p in params.values())


def test_every_progress_field_is_read_by_some_check():
    """The entry rule for Progress. listings_stale once sat there and no
    condition read it, which is how a report statistic ends up looking
    like a stopping criterion."""
    import dataclasses
    import inspect

    from crawlme.scheduler import stop_conds
    from crawlme.state.context import Progress

    source = inspect.getsource(stop_conds)
    unread = [f.name for f in dataclasses.fields(Progress) if f"p.{f.name}" not in source]
    assert unread == [], f"Progress fields nothing stops on: {unread}"
