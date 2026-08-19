"""Frontier: the shell around a WorkSource.

The Frontier is the crawler's scheduling hub: it owns the set of URLs
waiting to be fetched and decides which one goes next.  It does not call
AI and does not know page content; it only manages state.

Ordering lives in a WorkSource (see work_source.py).  Everything below is
traversal-independent, which is the point: a feed cursor drops in as a
different source and inherits all of it instead of growing a second copy.
See docs/refactor.md G2.

Gating (two levels)
-------------------
Per-item gate: each FrontierItem has next_available_at, set by retry
  backoff (exponential delay after 429/503) and by crawl-delay (minimum
  interval between requests to one domain).  An item that is not due yet
  is deferred and offered again later.

Per-domain gate: the optional next_allowed callback (typically backed by
  RobotsPolicy) answers "when is this domain next allowed?".  A cooling
  domain defers its items with an updated next_available_at.

Budget enforcement
------------------
Domain budget: past domain_budget successful fetches from one domain,
  further items from it are dropped rather than deferred; they are never
  coming back.

Global budget: past global_budget fetches overall, the scan stops for
  everyone and pop_next returns None, signalling the scheduler to stop.

Snapshot / restore
------------------
snapshot() serialises the source's ordering plus the visited set, budget
counters and sequence number into a FrontierSnapshot; restore() puts them
back.  This is the foundation of pause/resume and crash recovery."""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable
from typing import Any, Protocol

from crawlme.pioneer.prefilter import PreFilterContext
from crawlme.pioneer.work_source import Gate, GateFn, PriorityHeapSource, WorkSource
from crawlme.schemas import FrontierItem, FrontierItemStatus, FrontierSnapshot

logger = logging.getLogger(__name__)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Frontier(Protocol):
    """Contract for the priority-queue URL frontier."""

    @property
    def size(self) -> int: ...

    async def push_batch(self, items: list[FrontierItem]) -> None: ...

    async def pop_next(
        self,
        now: datetime.datetime | None = None,
        next_allowed: Callable[[str], datetime.datetime] | None = None,
        global_budget: int | None = None,
    ) -> FrontierItem | None: ...

    async def record_outcome(self, item: FrontierItem, status: FrontierItemStatus) -> None: ...

    def snapshot(self, task_id: str = "") -> FrontierSnapshot: ...

    def restore(self, snap: FrontierSnapshot) -> None: ...

    def get_prefilter_context(self, **overrides: Any) -> PreFilterContext: ...


class PriorityFrontier:
    """Gating, budgets, dedup and checkpoints over a pluggable ordering."""

    def __init__(
        self,
        domain_budget: int = 50,
        aging_window: float = 600.0,
        age_factor: float = 1.0,
        source: WorkSource | None = None,
    ) -> None:
        # Zero means no per-domain ceiling.  One is right for a link
        # graph, where a single site can otherwise absorb the whole run;
        # it is wrong for a feed, where every candidate shares the
        # platform's domain and the ceiling becomes a hidden total that
        # quietly overrides the page budget.
        self._domain_budget = domain_budget
        # How many candidates that ceiling turned away.  A frontier can
        # be empty because there was nothing left or because everything
        # left was refused, and a run that cannot tell the difference
        # reports the second as completion.
        self.blocked_by_domain_budget = 0
        self._lock = asyncio.Lock()
        self._source: WorkSource = source or PriorityHeapSource(
            aging_window=aging_window,
            age_factor=age_factor,
        )
        self._visited: set[str] = set()
        self._domain_counters: dict[str, int] = {}
        self._global_counter: int = 0

    async def push_batch(self, items: list[FrontierItem]) -> None:
        async with self._lock:
            fresh = [i for i in items if i.url_key not in self._visited and not self._source.contains(i.url_key)]
            await self._source.add(fresh)

    async def pop_next(
        self,
        now: datetime.datetime | None = None,
        next_allowed: Callable[[str], datetime.datetime] | None = None,
        global_budget: int | None = None,
    ) -> FrontierItem | None:
        """Return the highest-priority item that may be fetched right now."""
        now = now or _utcnow()
        async with self._lock:
            return await self._source.take(now, self._gate(next_allowed, global_budget))

    def _gate(
        self,
        next_allowed: Callable[[str], datetime.datetime] | None,
        global_budget: int | None,
    ) -> GateFn:
        """Decide one item's fate, knowing nothing about the ordering.

        The source calls this while scanning, because only it knows what
        comes next, and only the shell knows about delays and budgets.
        """

        def gate(item: FrontierItem, now: datetime.datetime) -> Gate:
            if item.next_available_at > now:
                return Gate.DEFER
            if next_allowed is not None:
                allowed_at = next_allowed(item.reg_domain)
                if allowed_at > now:
                    item.next_available_at = allowed_at
                    return Gate.DEFER
            used = self._domain_counters.get(item.reg_domain, 0)
            if self._domain_budget > 0 and used >= self._domain_budget:
                logger.warning(
                    "frontier.domain_budget domain=%s used=%d/%d",
                    item.reg_domain,
                    used,
                    self._domain_budget,
                )
                self.blocked_by_domain_budget += 1
                return Gate.DROP
            if global_budget is not None and global_budget > 0 and self._global_counter >= global_budget:
                return Gate.STOP
            return Gate.TAKE

        return gate

    async def record_outcome(self, item: FrontierItem, status: FrontierItemStatus) -> None:
        async with self._lock:
            item.status = status
            self._visited.add(item.url_key)
            self._source.discard(item.url_key)
            if status == "COMPLETED":
                self._domain_counters[item.reg_domain] = self._domain_counters.get(item.reg_domain, 0) + 1
                self._global_counter += 1

    async def mark_visited(self, url_key: str) -> None:
        async with self._lock:
            self._visited.add(url_key)

    def contains(self, url_key: str) -> bool:
        return url_key in self._visited or self._source.contains(url_key)

    def get_prefilter_context(self, **overrides: Any) -> PreFilterContext:
        """Expose the state PreFilter needs without handing out internals.

        allow_fetch and allowed_domains are owned by the scheduler (robots
        policy, CLI) and arrive as overrides rather than being stored here.
        """
        kwargs: dict[str, Any] = {
            "visited": self._visited.copy(),
            "frontier_keys": self._source.keys(),
            "domain_counters": dict(self._domain_counters),
        }
        kwargs.update(overrides)
        return PreFilterContext(**kwargs)

    @property
    def size(self) -> int:
        return self._source.size

    def snapshot(self, task_id: str = "") -> FrontierSnapshot:
        state = self._source.dump()
        return FrontierSnapshot(
            task_id=task_id,
            heap=state.get("heap", []),
            pending=state.get("pending", []),
            visited=self._visited.copy(),
            budgets={"domain": dict(self._domain_counters), "global": self._global_counter},
            counters={"seq": state.get("seq", 0)},
        )

    def restore(self, snap: FrontierSnapshot) -> None:
        self._visited = snap.visited.copy()
        self._domain_counters = dict(snap.budgets.get("domain", {}))
        self._global_counter = snap.budgets.get("global", 0)
        self._source.load(
            {
                "heap": list(snap.heap),
                "pending": list(snap.pending),
                "seq": snap.counters.get("seq", 0),
            }
        )
