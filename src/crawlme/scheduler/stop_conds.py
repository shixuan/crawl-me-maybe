"""Stop conditions: independent checks, each returning StopReason or None.

check_stop() runs them all each iteration and returns every triggered reason.
The scheduler decides whether to pause (USER_REQUESTED) or terminate (everything
else).

Every check in _CHECKS must be reachable.  A check whose input is never
written is worse than no check, because the capability looks present in
the docs while nothing can trigger it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from crawlme.digest.feed.base import PageProblem
from crawlme.pioneer.frontier import Frontier
from crawlme.schemas import CrawlTask
from crawlme.state.context import RELEVANCE_WINDOW, CrawlCounters

# Fewer than this many relevant pages in a full window means the crawl
# has stopped finding anything worth the budget.
_MIN_RELEVANT_IN_WINDOW = 2


@dataclass
class StopReason:
    code: str
    detail: str = ""


# individual checks ---------------------------------------------------

# All checks share the same signature so _CHECKS is a flat list.
_CheckFunc = Callable[[CrawlTask, Frontier, CrawlCounters], StopReason | None]


# Listings a run must have read before "all of them were empty" means
# anything.  One or two quiet accounts is a normal week.
_EMPTY_LISTING_FLOOR = 3


def _budget_pages(
    _task: CrawlTask,
    _frontier: Frontier,
    c: CrawlCounters,
) -> StopReason | None:
    if c.max_pages > 0 and c.pages_fetched >= c.max_pages:
        return StopReason("BUDGET_PAGES", f"fetched {c.pages_fetched}/{c.max_pages} pages")
    return None


def _budget_tokens(
    _task: CrawlTask,
    _frontier: Frontier,
    c: CrawlCounters,
) -> StopReason | None:
    if c.max_tokens > 0 and c.tokens_used >= c.max_tokens:
        return StopReason("BUDGET_TOKENS", f"used {c.tokens_used}/{c.max_tokens} tokens")
    return None


def _budget_time(
    _task: CrawlTask,
    _frontier: Frontier,
    c: CrawlCounters,
) -> StopReason | None:
    if c.max_duration_sec > 0 and c.started_at > 0 and (time.monotonic() - c.started_at) >= c.max_duration_sec:
        return StopReason("BUDGET_TIME", f"ran {c.max_duration_sec}s")
    return None


def _time_horizon(
    _task: CrawlTask,
    _frontier: Frontier,
    c: CrawlCounters,
) -> StopReason | None:
    """Stop once the content has aged out of the goal's window.

    Dormant unless the goal carries a `since`, so every run that does not
    ask for a window behaves exactly as before.

    The premise is reverse-chronological traversal: the first run of
    pages older than the window means everything after it is older too.
    That holds within one feed, listing page, or archive, and passing
    `--since` is the user asserting their source reads that way.

    It stops holding the moment a run has more than one entry point.
    Monitoring thirty shops interleaves thirty traversals, so "pages in a
    row" spans accounts that have nothing to do with each other: one
    quiet shop's back catalogue would end the run before an active shop's
    posts were ever reached.  Losing those results is far worse than
    spending the budget, so the streak arms only where it can be read at
    face value, and anything else leaves it dormant.

    Dropping stale candidates one at a time is the part that still works
    everywhere; PreFilter's `stale_check` does it whenever a listing
    stated the date.  See refactor.md R3.
    """
    # Two conditions, two reasons.  The traversal says whether its
    # source is ordered by time at all; the seed count says whether this
    # run walks one of them or interleaves several, which turns "pages in
    # a row" into pages from sources that have nothing to do with each
    # other.
    if c.since is None or c.max_stale_streak <= 0:
        return None
    if not c.time_horizon_allowed or c.seed_count != 1:
        return None
    if c.stale_streak >= c.max_stale_streak:
        return StopReason(
            "TIME_HORIZON",
            f"{c.stale_streak} pages in a row older than {c.since.date().isoformat()}",
        )
    return None


def _is_drained(frontier: Frontier, c: CrawlCounters) -> bool:
    """Nothing to fetch in either half, and nothing on its way back."""
    return frontier.size == 0 and frontier.waiting.is_empty and c.in_flight == 0 and frontier.scoring == 0


def _frontier_drained(
    _task: CrawlTask,
    frontier: Frontier,
    c: CrawlCounters,
) -> StopReason | None:
    """The crawl read everything it found."""
    if not _is_drained(frontier, c):
        return None
    return StopReason("FRONTIER_DRAINED", "no more URLs to fetch")


def _ceiling_refused(
    _task: CrawlTask,
    frontier: Frontier,
    c: CrawlCounters,
) -> StopReason | None:
    """The per-domain ceiling refused candidates before the run ended.

    Said alongside FRONTIER_DRAINED rather than instead of it, because
    both are true and only together do they answer why nothing is left.
    A feed crawl ended at fifty pages with a hundred and sixty
    candidates refused and reported only "completed"; a graph crawl that
    genuinely exhausts itself refuses thousands along the way and is
    still a real completion.
    """
    blocked = getattr(frontier, "blocked_by_domain_budget", 0)
    if not blocked or not _is_drained(frontier, c):
        return None
    return StopReason("DOMAIN_BUDGET", f"{blocked} candidates refused by the per-domain ceiling")


def _enough_found(
    _task: CrawlTask,
    _frontier: Frontier,
    c: CrawlCounters,
) -> StopReason | None:
    """Stop once the run has what it was asked for.

    The other stop conditions are ceilings on what a run may spend; this
    one is the only statement of what it is for.  Without it a page
    budget has to stand in for a goal, and "sixty pages" tells nobody
    how many answers that buys -- one run spent sixty and returned
    twenty-two.

    Analysis lags fetching, so the tally can pass the target by whatever
    was already in flight.  Overshooting by a page or two beats holding
    the pumps to make the count exact.
    """
    if c.max_relevant > 0 and c.relevant_found >= c.max_relevant:
        return StopReason("MAX_RELEVANT", f"found {c.relevant_found}/{c.max_relevant} relevant pages")
    return None


def _diminishing_returns(
    _task: CrawlTask,
    _frontier: Frontier,
    c: CrawlCounters,
) -> StopReason | None:
    # --recall means the run was asked to read the candidates the
    # ranker rejected, and it reads them last.  A tail of misses is
    # therefore the point of the mode, not evidence the crawl is
    # finished, and stopping on it cuts off exactly the stretch the run
    # was made to measure.
    if c.recall:
        return None
    window = c.relevance_window
    if len(window) >= RELEVANCE_WINDOW and sum(window) < _MIN_RELEVANT_IN_WINDOW:
        return StopReason("DIMINISHING_RETURNS", f"only {sum(window)} relevant in last {len(window)}")
    return None


def _platform_refused(
    _task: CrawlTask,
    _frontier: Frontier,
    c: CrawlCounters,
) -> StopReason | None:
    """The platform is refusing this crawl, not just this page.

    Rate limiting and an expired session are facts about the crawler,
    so the first one settles every request that would follow: they
    would all be refused too, and on a platform that counts strikes,
    asking again is how a session becomes a ban.  Stopping on the first
    one trades a re-run for that risk.

    A gone account is the opposite kind of fact and never arrives here;
    it is counted and reported instead.  See PageProblem.refuses_the_run.
    """
    if not c.refused_by:
        return None
    if c.refused_by == PageProblem.LOGIN_REQUIRED.value:
        return StopReason("LOGIN_REQUIRED", "the platform asked for a login; the session is not valid")
    return StopReason("RATE_LIMITED", f"the platform refused the crawl ({c.refused_by})")


def _adapter_empty(
    _task: CrawlTask,
    frontier: Frontier,
    c: CrawlCounters,
) -> StopReason | None:
    """Every listing was readable and none of them held anything.

    That is what a platform redesign looks like from inside: the pages
    still arrive, the adapter still recognises them as pages, and it
    recognises nothing on any of them.  The run then drains on schedule
    and reports a finished crawl of a platform that posted nothing.

    Said alongside FRONTIER_DRAINED rather than instead of it, and only
    once the run is over: a single empty account is an account having a
    quiet week, and mid-run there is no telling which this is.
    """
    if not _is_drained(frontier, c):
        return None
    if c.listings_seen < _EMPTY_LISTING_FLOOR or c.listings_empty < c.listings_seen:
        return None
    return StopReason("ADAPTER_EMPTY", f"all {c.listings_seen} listings parsed and none held an item")


def _user_requested(
    task: CrawlTask,
    _frontier: Frontier,
    _counters: CrawlCounters,
) -> StopReason | None:
    if task.state == "STOPPING":
        return StopReason("USER_REQUESTED", "stop requested by user")
    return None


def _fatal(
    _task: CrawlTask,
    _frontier: Frontier,
    c: CrawlCounters,
) -> StopReason | None:
    if c.fatal_error:
        return StopReason("FATAL", c.fatal_error)
    return None


# main entry ----------------------------------------------------------

_CHECKS: list[_CheckFunc] = [
    _budget_pages,
    _budget_tokens,
    _budget_time,
    _time_horizon,
    _fatal,
    _platform_refused,
    _adapter_empty,
    _user_requested,
    _enough_found,
    _diminishing_returns,
    _frontier_drained,
    _ceiling_refused,
]


def check_stop(
    task: CrawlTask,
    frontier: Frontier,
    counters: CrawlCounters,
) -> list[StopReason]:
    reasons: list[StopReason] = []
    for check in _CHECKS:
        result = check(task, frontier, counters)
        if result is not None:
            reasons.append(result)
    return reasons
