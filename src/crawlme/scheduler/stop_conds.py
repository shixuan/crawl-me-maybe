"""Pure run stopping policies over progress and limits."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from crawlme.platforms import PageProblem
from crawlme.runtime import Limits, Progress


@dataclass
class StopReason:
    code: str
    detail: str = ""


_FETCH_EXHAUSTION = {"BUDGET_PAGES", "FRONTIER_DRAINED", "DOMAIN_BUDGET", "ADAPTER_EMPTY"}


def can_drain_analysis(reasons: Sequence[StopReason]) -> bool:
    """Fetch exhaustion still permits analysis of pages already retrieved."""
    return all(r.code in _FETCH_EXHAUSTION for r in reasons)


def can_settle_pages(reasons: Sequence[StopReason]) -> bool:
    """Dispatched pages may finish after fetch exhaustion or meeting the result target."""
    return all(r.code in _FETCH_EXHAUSTION | {"MAX_RELEVANT"} for r in reasons)


# Listings a run must have read before "all of them were empty" means
# anything.  One or two quiet accounts is a normal week.
_EMPTY_LISTING_FLOOR = 3


def _budget_pages(
    p: Progress,
    lim: Limits,
) -> StopReason | None:
    if lim.max_pages > 0 and p.pages_fetched >= lim.max_pages:
        return StopReason("BUDGET_PAGES", f"fetched {p.pages_fetched}/{lim.max_pages} pages")
    return None


def _budget_tokens(
    tokens_used: int,
    lim: Limits,
) -> StopReason | None:
    if lim.max_tokens > 0 and tokens_used >= lim.max_tokens:
        return StopReason("BUDGET_TOKENS", f"used {tokens_used}/{lim.max_tokens} tokens")
    return None


def _budget_time(
    p: Progress,
    lim: Limits,
) -> StopReason | None:
    if lim.max_duration_sec > 0 and p.started_at > 0 and (time.monotonic() - p.started_at) >= lim.max_duration_sec:
        return StopReason("BUDGET_TIME", f"ran {lim.max_duration_sec}s")
    return None


def _frontier_drained(drained: bool) -> StopReason | None:
    if drained:
        return StopReason("FRONTIER_DRAINED", "no more URLs to fetch")
    return None


def _ceiling_refused(drained: bool, blocked: int) -> StopReason | None:
    if drained and blocked:
        return StopReason("DOMAIN_BUDGET", f"{blocked} candidates refused by the per-domain ceiling")
    return None


def _enough_found(
    p: Progress,
    lim: Limits,
) -> StopReason | None:
    """Stop dispatch when the result target is met; in-flight analyses may still complete."""
    if lim.max_relevant > 0 and p.relevant_found >= lim.max_relevant:
        return StopReason("MAX_RELEVANT", f"found {p.relevant_found}/{lim.max_relevant} relevant pages")
    return None


def _platform_refused(
    p: Progress,
    lim: Limits,
) -> StopReason | None:
    """Stop on run-wide platform refusal; unavailable individual pages do not trigger this."""
    if not p.refused_by:
        return None
    if p.refused_by == PageProblem.LOGIN_REQUIRED.value:
        return StopReason("LOGIN_REQUIRED", "the platform asked for a login; the session is not valid")
    return StopReason("RATE_LIMITED", f"the platform refused the crawl ({p.refused_by})")


def _adapter_empty(
    p: Progress,
    drained: bool,
) -> StopReason | None:
    """Flag a drained run whose listings all yielded no candidates, after the minimum sample."""
    if not drained:
        return None
    if p.listings_seen < _EMPTY_LISTING_FLOOR or p.listings_empty < p.listings_seen:
        return None
    return StopReason("ADAPTER_EMPTY", f"all {p.listings_seen} listings parsed and none held an item")


def _fatal(
    p: Progress,
    lim: Limits,
) -> StopReason | None:
    if p.fatal_error:
        return StopReason("FATAL", p.fatal_error)
    return None


def check_stop(
    progress: Progress,
    limits: Limits,
    *,
    tokens_used: int,
    queued: int,
    waiting: int,
    ranking: int,
    domain_refused: int,
) -> list[StopReason]:
    """Evaluate stopping policies from run counters and the owners' current counts."""
    drained = queued == 0 and waiting == 0 and progress.in_flight == 0 and ranking == 0
    results = (
        _budget_pages(progress, limits),
        _budget_tokens(tokens_used, limits),
        _budget_time(progress, limits),
        _fatal(progress, limits),
        _platform_refused(progress, limits),
        _adapter_empty(progress, drained),
        _enough_found(progress, limits),
        _frontier_drained(drained),
        _ceiling_refused(drained, domain_refused),
    )
    return [reason for reason in results if reason is not None]
