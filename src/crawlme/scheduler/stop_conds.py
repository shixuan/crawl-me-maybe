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

from crawlme.pioneer.buffer import Buffer
from crawlme.pioneer.frontier import Frontier
from crawlme.schemas import CrawlTask
from crawlme.state.context import RELEVANCE_WINDOW, CrawlCounters

#: Fewer than this many relevant pages in a full window means the crawl
#: has stopped finding anything worth the budget.
_MIN_RELEVANT_IN_WINDOW = 2


@dataclass
class StopReason:
    code: str
    detail: str = ""


#: individual checks ---------------------------------------------------

# All checks share the same signature so _CHECKS is a flat list.
_CheckFunc = Callable[[CrawlTask, Frontier, Buffer, CrawlCounters], StopReason | None]


def _budget_pages(
    _task: CrawlTask,
    _frontier: Frontier,
    _buffer: Buffer,
    c: CrawlCounters,
) -> StopReason | None:
    if c.max_pages > 0 and c.pages_fetched >= c.max_pages:
        return StopReason("BUDGET_PAGES", f"fetched {c.pages_fetched}/{c.max_pages} pages")
    return None


def _budget_tokens(
    _task: CrawlTask,
    _frontier: Frontier,
    _buffer: Buffer,
    c: CrawlCounters,
) -> StopReason | None:
    if c.max_tokens > 0 and c.tokens_used >= c.max_tokens:
        return StopReason("BUDGET_TOKENS", f"used {c.tokens_used}/{c.max_tokens} tokens")
    return None


def _budget_time(
    _task: CrawlTask,
    _frontier: Frontier,
    _buffer: Buffer,
    c: CrawlCounters,
) -> StopReason | None:
    if c.max_duration_sec > 0 and c.started_at > 0 and (time.monotonic() - c.started_at) >= c.max_duration_sec:
        return StopReason("BUDGET_TIME", f"ran {c.max_duration_sec}s")
    return None


def _time_horizon(
    _task: CrawlTask,
    _frontier: Frontier,
    _buffer: Buffer,
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
    if c.since is None or c.max_stale_streak <= 0 or c.seed_count != 1:
        return None
    if c.stale_streak >= c.max_stale_streak:
        return StopReason(
            "TIME_HORIZON",
            f"{c.stale_streak} pages in a row older than {c.since.date().isoformat()}",
        )
    return None


def _frontier_drained(
    _task: CrawlTask,
    frontier: Frontier,
    buffer: Buffer,
    c: CrawlCounters,
) -> StopReason | None:
    if frontier.size == 0 and buffer.is_empty and c.in_flight == 0:
        return StopReason("FRONTIER_DRAINED", "no more URLs to fetch")
    return None


def _diminishing_returns(
    _task: CrawlTask,
    _frontier: Frontier,
    _buffer: Buffer,
    c: CrawlCounters,
) -> StopReason | None:
    window = c.relevance_window
    if len(window) >= RELEVANCE_WINDOW and sum(window) < _MIN_RELEVANT_IN_WINDOW:
        return StopReason("DIMINISHING_RETURNS", f"only {sum(window)} relevant in last {len(window)}")
    return None


def _user_requested(
    task: CrawlTask,
    _frontier: Frontier,
    _buffer: Buffer,
    _counters: CrawlCounters,
) -> StopReason | None:
    if task.state == "STOPPING":
        return StopReason("USER_REQUESTED", "stop requested by user")
    return None


def _fatal(
    _task: CrawlTask,
    _frontier: Frontier,
    _buffer: Buffer,
    c: CrawlCounters,
) -> StopReason | None:
    if c.fatal_error:
        return StopReason("FATAL", c.fatal_error)
    return None


#: main entry ----------------------------------------------------------

_CHECKS: list[_CheckFunc] = [
    _budget_pages,
    _budget_tokens,
    _budget_time,
    _time_horizon,
    _fatal,
    _user_requested,
    _diminishing_returns,
    _frontier_drained,
]


def check_stop(
    task: CrawlTask,
    frontier: Frontier,
    buffer: Buffer,
    counters: CrawlCounters,
) -> list[StopReason]:
    reasons: list[StopReason] = []
    for check in _CHECKS:
        result = check(task, frontier, buffer, counters)
        if result is not None:
            reasons.append(result)
    return reasons
