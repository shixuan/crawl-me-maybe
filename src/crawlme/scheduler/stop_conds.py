"""Stop conditions — 8 independent checks, each returning StopReason or None.

check_stop() runs them all each iteration and returns every triggered reason.
The scheduler decides whether to pause (USER_REQUESTED) or terminate (everything
else).

Counters dict — maintained by the scheduler across iterations:
  max_pages, max_tokens, max_duration_sec  — budget limits (set at start)
  min_relevant_hits, relevance_threshold   — goal-satisfaction targets
  pages_fetched, tokens_used               — running totals
  started_at                               — epoch timestamp (monotonic)
  relevance_window                         — list[bool], last 20 pages
  in_flight                                — fetches currently in progress
  fatal_error                              — set on unrecoverable failure
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from crawlme.pioneer.buffer import CandidateBuffer
from crawlme.pioneer.frontier import Frontier
from crawlme.schemas import CrawlTask


@dataclass
class StopReason:
    code: str
    detail: str = ""


# -- individual checks ---------------------------------------------------

# All checks share the same signature so _CHECKS is a flat list.
_CheckFunc = Callable[[CrawlTask, Frontier, CandidateBuffer, dict[str, Any]], StopReason | None]


def _budget_pages(
    _task: CrawlTask, _frontier: Frontier, _buffer: CandidateBuffer, counters: dict[str, Any]
) -> StopReason | None:
    limit = counters.get("max_pages", 0)
    fetched = counters.get("pages_fetched", 0)
    if limit > 0 and fetched >= limit:
        return StopReason("BUDGET_PAGES", f"fetched {fetched}/{limit} pages")
    return None


def _budget_tokens(
    _task: CrawlTask, _frontier: Frontier, _buffer: CandidateBuffer, counters: dict[str, Any]
) -> StopReason | None:
    limit = counters.get("max_tokens", 0)
    used = counters.get("tokens_used", 0)
    if limit > 0 and used >= limit:
        return StopReason("BUDGET_TOKENS", f"used {used}/{limit} tokens")
    return None


def _budget_time(
    _task: CrawlTask, _frontier: Frontier, _buffer: CandidateBuffer, counters: dict[str, Any]
) -> StopReason | None:
    limit = counters.get("max_duration_sec", 0)
    started = counters.get("started_at", 0.0)
    if limit > 0 and started > 0 and (time.monotonic() - started) >= limit:
        return StopReason("BUDGET_TIME", f"ran {limit}s")
    return None


def _frontier_drained(
    _task: CrawlTask, frontier: Frontier, buffer: CandidateBuffer, counters: dict[str, Any]
) -> StopReason | None:
    if frontier.size == 0 and buffer.is_empty and counters.get("in_flight", 0) == 0:
        return StopReason("FRONTIER_DRAINED", "no more URLs to fetch")
    return None


def _goal_satisfied(
    _task: CrawlTask, _frontier: Frontier, _buffer: CandidateBuffer, counters: dict[str, Any]
) -> StopReason | None:
    hits = counters.get("relevant_hits", 0)
    needed = counters.get("min_relevant_hits", 0)
    threshold = counters.get("relevance_threshold", 0.0)
    avg = counters.get("avg_relevance", 0.0)
    if needed > 0 and hits >= needed and avg >= threshold:
        return StopReason("GOAL_SATISFIED", f"{hits} relevant pages, avg={avg:.2f}")
    return None


def _diminishing_returns(
    _task: CrawlTask, _frontier: Frontier, _buffer: CandidateBuffer, counters: dict[str, Any]
) -> StopReason | None:
    window: list[bool] = counters.get("relevance_window", [])
    if len(window) >= 20 and sum(window) < 2:
        return StopReason("DIMINISHING_RETURNS", f"only {sum(window)} relevant in last {len(window)}")
    return None


def _user_requested(
    task: CrawlTask, _frontier: Frontier, _buffer: CandidateBuffer, _counters: dict[str, Any]
) -> StopReason | None:
    if task.state == "STOPPING":
        return StopReason("USER_REQUESTED", "stop requested by user")
    return None


def _fatal(
    _task: CrawlTask, _frontier: Frontier, _buffer: CandidateBuffer, counters: dict[str, Any]
) -> StopReason | None:
    err = counters.get("fatal_error", "")
    if err:
        return StopReason("FATAL", str(err))
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
    buffer: CandidateBuffer,
    counters: dict[str, Any],
) -> list[StopReason]:
    reasons: list[StopReason] = []
    for check in _CHECKS:
        result = check(task, frontier, buffer, counters)
        if result is not None:
            reasons.append(result)
    return reasons
