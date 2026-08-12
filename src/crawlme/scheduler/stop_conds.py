"""Stop conditions — 8 independent checks, each returning StopReason or None.

check_stop() runs them all each iteration and returns every triggered reason.
The scheduler decides whether to pause (USER_REQUESTED) or terminate (everything
else).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from crawlme.pioneer.buffer import Buffer
from crawlme.pioneer.frontier import Frontier
from crawlme.schemas import CrawlCounters, CrawlTask


@dataclass
class StopReason:
    code: str
    detail: str = ""


# -- individual checks ---------------------------------------------------

# All checks share the same signature so _CHECKS is a flat list.
_CheckFunc = Callable[[CrawlTask, Frontier, Buffer, CrawlCounters], StopReason | None]


def _budget_pages(
    _task: CrawlTask, _frontier: Frontier, _buffer: Buffer, c: CrawlCounters,
) -> StopReason | None:
    if c.max_pages > 0 and c.pages_fetched >= c.max_pages:
        return StopReason("BUDGET_PAGES", f"fetched {c.pages_fetched}/{c.max_pages} pages")
    return None


def _budget_tokens(
    _task: CrawlTask, _frontier: Frontier, _buffer: Buffer, c: CrawlCounters,
) -> StopReason | None:
    if c.max_tokens > 0 and c.tokens_used >= c.max_tokens:
        return StopReason("BUDGET_TOKENS", f"used {c.tokens_used}/{c.max_tokens} tokens")
    return None


def _budget_time(
    _task: CrawlTask, _frontier: Frontier, _buffer: Buffer, c: CrawlCounters,
) -> StopReason | None:
    if c.max_duration_sec > 0 and c.started_at > 0 and (time.monotonic() - c.started_at) >= c.max_duration_sec:
        return StopReason("BUDGET_TIME", f"ran {c.max_duration_sec}s")
    return None


def _frontier_drained(
    _task: CrawlTask, frontier: Frontier, buffer: Buffer, c: CrawlCounters,
) -> StopReason | None:
    if frontier.size == 0 and buffer.is_empty and c.in_flight == 0:
        return StopReason("FRONTIER_DRAINED", "no more URLs to fetch")
    return None


def _goal_satisfied(
    _task: CrawlTask, _frontier: Frontier, _buffer: Buffer, c: CrawlCounters,
) -> StopReason | None:
    hits = c.relevance_window.count(True)  # best-effort from window
    if c.min_relevant_hits > 0 and hits >= c.min_relevant_hits:
        return StopReason("GOAL_SATISFIED", f"{hits} relevant pages in recent window")
    return None


def _diminishing_returns(
    _task: CrawlTask, _frontier: Frontier, _buffer: Buffer, c: CrawlCounters,
) -> StopReason | None:
    window = c.relevance_window
    if len(window) >= 20 and sum(window) < 2:
        return StopReason("DIMINISHING_RETURNS", f"only {sum(window)} relevant in last {len(window)}")
    return None


def _user_requested(
    task: CrawlTask, _frontier: Frontier, _buffer: Buffer, _counters: CrawlCounters,
) -> StopReason | None:
    if task.state == "STOPPING":
        return StopReason("USER_REQUESTED", "stop requested by user")
    return None


def _fatal(
    _task: CrawlTask, _frontier: Frontier, _buffer: Buffer, c: CrawlCounters,
) -> StopReason | None:
    if c.fatal_error:
        return StopReason("FATAL", c.fatal_error)
    return None


# -- main entry ----------------------------------------------------------

_CHECKS: list[_CheckFunc] = [
    _budget_pages,
    _budget_tokens,
    _budget_time,
    _fatal,
    _user_requested,
    _goal_satisfied,
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
