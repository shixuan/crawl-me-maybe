from __future__ import annotations

import dataclasses
import time

import pytest

from crawlme.runtime import Limits, Progress
from crawlme.scheduler.stop_conds import can_drain_analysis, check_stop


def _frontier(size: int = 0, scoring: int = 0, waiting: int = 0) -> dict:
    return dict(queued=size, ranking=scoring, waiting=waiting, domain_refused=0)


def _check(frontier: dict, limits: Limits, progress: Progress, tokens_used: int = 0):
    return check_stop(progress, limits, tokens_used=tokens_used, **frontier)


def _split(**kw) -> tuple[Limits, Progress, int]:
    lim = {k: v for k, v in kw.items() if k in _LIMIT_FIELDS}
    progress = Progress(**{k: v for k, v in kw.items() if k not in _LIMIT_FIELDS and k != "tokens_used"})
    return Limits(**lim), progress, kw.get("tokens_used", 0)


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


@pytest.mark.parametrize(
    ("limits", "progress", "allowed"),
    [
        ({"max_pages": 1}, {"pages_fetched": 1}, True),
        ({"max_pages": 1, "max_relevant": 1}, {"pages_fetched": 1, "relevant_found": 1}, False),
        ({"max_pages": 1}, {"pages_fetched": 1, "fatal_error": "failed"}, False),
        ({"max_pages": 1}, {"pages_fetched": 1, "refused_by": "LOGIN_REQUIRED"}, False),
    ],
)
def test_analysis_after_fetch_stop(limits, progress, allowed):
    reasons = _check(_frontier(), Limits(**limits), Progress(**progress))
    assert can_drain_analysis(reasons) is allowed


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
    assert ("BUDGET_PAGES" in _codes(_check(_frontier(), *_split(**kw)))) is fires


@pytest.mark.parametrize(
    ("kw", "fires"),
    [
        ({"max_tokens": 5000, "tokens_used": 5000}, True),
        ({"max_tokens": 5000, "tokens_used": 4999}, False),
        ({"max_tokens": 0, "tokens_used": 999999}, False),
    ],
)
def test_budget_tokens(kw, fires):
    assert ("BUDGET_TOKENS" in _codes(_check(_frontier(), *_split(**kw)))) is fires


@pytest.mark.parametrize(
    ("max_duration_sec", "elapsed", "fires"),
    [(1, 10, True), (3600, 0, False)],
)
def test_budget_time(max_duration_sec, elapsed, fires):
    lim, prog, _ = _split(max_duration_sec=max_duration_sec, started_at=time.monotonic() - elapsed)
    assert ("BUDGET_TIME" in _codes(_check(_frontier(), lim, prog))) is fires


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
    reasons = _check(_frontier(**frontier_kw), *_split(in_flight=in_flight))
    assert ("FRONTIER_DRAINED" in _codes(reasons)) is fires


@pytest.mark.parametrize("blocked", [7, 0])
def test_ceiling_named(blocked):
    """Report domain refusals alongside natural exhaustion."""
    frontier = _frontier(size=0)
    frontier["domain_refused"] = blocked
    codes = _codes(_check(frontier, *_split(in_flight=0)))
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
    lim, prog, _ = _split(max_relevant=max_relevant, relevant_found=found)
    assert ("MAX_RELEVANT" in _codes(_check(_frontier(size=9), lim, prog))) is fires


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
    codes = _codes(_check(_frontier(), *_split(refused_by=refused_by)))
    refusals = [c for c in codes if c in ("RATE_LIMITED", "LOGIN_REQUIRED")]
    assert refusals == ([code] if code else [])


# -- user, fatal, and everything at once ---------------------------------


def test_fatal():
    assert "FATAL" in _codes(_check(_frontier(), *_split(fatal_error="disk full")))


def test_many_reasons():
    """Every check runs; the run reports all of them, not the first."""
    codes = set(_codes(_check(_frontier(), *_split(max_pages=10, pages_fetched=10, fatal_error="disk full"))))
    assert {"BUDGET_PAGES", "FATAL"} <= codes


def test_healthy_quiet():
    reasons = _check(
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
    lim, prog, _ = _split(in_flight=0, listings_seen=seen, listings_empty=empty)
    assert ("ADAPTER_EMPTY" in _codes(_check(_frontier(), lim, prog))) is fires


def test_empty_waits():
    """Mid-run there is no telling a dead adapter from a slow start."""
    lim, prog, _ = _split(in_flight=2, listings_seen=5, listings_empty=5)
    assert "ADAPTER_EMPTY" not in _codes(_check(_frontier(size=3), lim, prog))


def test_a_stop_condition_cannot_see_the_ledger():
    """The split that keeps statistics out of the stopping criteria is
    held by this signature, not by care. A number nothing stops on used
    to sit among the ones that do, and adding another was one keyword
    away."""
    import inspect

    from crawlme.runtime import Stats

    params = inspect.signature(check_stop).parameters
    assert "stats" not in params
    assert not any(p.annotation is Stats for p in params.values())


def test_every_progress_field_is_read_by_some_check():
    """The entry rule for Progress. listings_stale once sat there and no
    condition read it, which is how a report statistic ends up looking
    like a stopping criterion."""
    import inspect

    from crawlme.runtime import Progress
    from crawlme.scheduler import stop_conds

    source = inspect.getsource(stop_conds)
    unread = [
        f.name
        for f in dataclasses.fields(Progress)
        if f"p.{f.name}" not in source and f"progress.{f.name}" not in source
    ]
    assert unread == [], f"Progress fields nothing stops on: {unread}"
