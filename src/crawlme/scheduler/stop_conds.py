"""Run stopping and individual source-retirement policies. The scheduler applies their decisions."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from crawlme.digest.feed.base import PageProblem
from crawlme.pioneer.frontier import Frontier
from crawlme.runtime.state import RELEVANCE_WINDOW, Limits, Progress
from crawlme.schemas import CrawlTask

# has stopped finding anything worth the budget.


@dataclass
class StopReason:
    code: str
    detail: str = ""


# individual checks ---------------------------------------------------

# -- one source ----------------------------------------------------------

# How few relevant pages in a full window mean a source has stopped
# paying off. How big the window is belongs to the state that holds it.
MIN_RELEVANT_IN_WINDOW = 2
# Consecutive pages older than the goal's window before a source reads as
# walked past its end.
MAX_STALE_STREAK = 5


def why_retire(window: Sequence[bool], stale: int) -> str | None:
    """Whether one source has stopped being worth reading, and why.

    The reason travels with the judgement, as a StopReason's does: a run
    that retires a source it should have kept has to be arguable.
    """
    if len(window) >= RELEVANCE_WINDOW and sum(window) < MIN_RELEVANT_IN_WINDOW:
        return f"nothing in its last {RELEVANCE_WINDOW} pages"
    if stale >= MAX_STALE_STREAK:
        return f"{stale} pages in a row older than the window"
    return None


# -- the run -------------------------------------------------------------

# All checks share the same signature so _CHECKS is a flat list.
_CheckFunc = Callable[[CrawlTask, Frontier, Limits, Progress], StopReason | None]


# Listings a run must have read before "all of them were empty" means
# anything.  One or two quiet accounts is a normal week.
_EMPTY_LISTING_FLOOR = 3


def _budget_pages(
    _task: CrawlTask,
    _frontier: Frontier,
    lim: Limits,
    p: Progress,
) -> StopReason | None:
    if lim.max_pages > 0 and p.pages_fetched >= lim.max_pages:
        return StopReason("BUDGET_PAGES", f"fetched {p.pages_fetched}/{lim.max_pages} pages")
    return None


def _budget_tokens(
    _task: CrawlTask,
    _frontier: Frontier,
    lim: Limits,
    p: Progress,
) -> StopReason | None:
    if lim.max_tokens > 0 and p.tokens_used >= lim.max_tokens:
        return StopReason("BUDGET_TOKENS", f"used {p.tokens_used}/{lim.max_tokens} tokens")
    return None


def _budget_time(
    _task: CrawlTask,
    _frontier: Frontier,
    lim: Limits,
    p: Progress,
) -> StopReason | None:
    if lim.max_duration_sec > 0 and p.started_at > 0 and (time.monotonic() - p.started_at) >= lim.max_duration_sec:
        return StopReason("BUDGET_TIME", f"ran {lim.max_duration_sec}s")
    return None


def _is_drained(frontier: Frontier, p: Progress) -> bool:
    """Nothing to fetch in either half, and nothing on its way back."""
    return frontier.size == 0 and frontier.waiting.is_empty and p.in_flight == 0 and frontier.scoring == 0


def _frontier_drained(
    _task: CrawlTask,
    frontier: Frontier,
    lim: Limits,
    p: Progress,
) -> StopReason | None:
    """The crawl read everything it found."""
    if not _is_drained(frontier, p):
        return None
    return StopReason("FRONTIER_DRAINED", "no more URLs to fetch")


def _ceiling_refused(
    _task: CrawlTask,
    frontier: Frontier,
    lim: Limits,
    p: Progress,
) -> StopReason | None:
    """Add domain-budget context when the frontier drains after refusing candidates."""
    blocked = getattr(frontier, "blocked_by_domain_budget", 0)
    if not blocked or not _is_drained(frontier, p):
        return None
    return StopReason("DOMAIN_BUDGET", f"{blocked} candidates refused by the per-domain ceiling")


def _enough_found(
    _task: CrawlTask,
    _frontier: Frontier,
    lim: Limits,
    p: Progress,
) -> StopReason | None:
    """Stop dispatch when the result target is met; in-flight analyses may still complete."""
    if lim.max_relevant > 0 and p.relevant_found >= lim.max_relevant:
        return StopReason("MAX_RELEVANT", f"found {p.relevant_found}/{lim.max_relevant} relevant pages")
    return None


def _platform_refused(
    _task: CrawlTask,
    _frontier: Frontier,
    lim: Limits,
    p: Progress,
) -> StopReason | None:
    """Stop on run-wide platform refusal; unavailable individual pages do not trigger this."""
    if not p.refused_by:
        return None
    if p.refused_by == PageProblem.LOGIN_REQUIRED.value:
        return StopReason("LOGIN_REQUIRED", "the platform asked for a login; the session is not valid")
    return StopReason("RATE_LIMITED", f"the platform refused the crawl ({p.refused_by})")


def _adapter_empty(
    _task: CrawlTask,
    frontier: Frontier,
    lim: Limits,
    p: Progress,
) -> StopReason | None:
    """Flag a drained run whose listings all yielded no candidates, after the minimum sample."""
    if not _is_drained(frontier, p):
        return None
    if p.listings_seen < _EMPTY_LISTING_FLOOR or p.listings_empty < p.listings_seen:
        return None
    return StopReason("ADAPTER_EMPTY", f"all {p.listings_seen} listings parsed and none held an item")


def _user_requested(
    task: CrawlTask,
    _frontier: Frontier,
    _lim: Limits,
    _p: Progress,
) -> StopReason | None:
    if task.state == "STOPPING":
        return StopReason("USER_REQUESTED", "stop requested by user")
    return None


def _fatal(
    _task: CrawlTask,
    _frontier: Frontier,
    lim: Limits,
    p: Progress,
) -> StopReason | None:
    if p.fatal_error:
        return StopReason("FATAL", p.fatal_error)
    return None


# main entry ----------------------------------------------------------

_CHECKS: list[_CheckFunc] = [
    _budget_pages,
    _budget_tokens,
    _budget_time,
    _fatal,
    _platform_refused,
    _adapter_empty,
    _user_requested,
    _enough_found,
    _frontier_drained,
    _ceiling_refused,
]


def check_stop(
    task: CrawlTask,
    frontier: Frontier,
    limits: Limits,
    progress: Progress,
) -> list[StopReason]:
    """Return all applicable stop reasons using limits and progress, excluding reporting-only state."""
    reasons: list[StopReason] = []
    for check in _CHECKS:
        result = check(task, frontier, limits, progress)
        if result is not None:
            reasons.append(result)
    return reasons
