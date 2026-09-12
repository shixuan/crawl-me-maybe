"""Frontier contract and implementation for queued and unranked candidates."""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable
from typing import Any, Protocol

from crawlme.pioneer.buffer import Buffer, RoundRobinBuffer
from crawlme.pioneer.prefilter import PreFilterContext
from crawlme.pioneer.queue import Gate, GateFn, PriorityQueue
from crawlme.schemas import Candidate, FrontierItem, FrontierItemStatus, FrontierSnapshot

logger = logging.getLogger(__name__)


class Frontier(Protocol):
    """Contract for the priority-queue URL frontier."""

    @property
    def size(self) -> int: ...

    # the unscored half: candidates waiting for someone to score them.
    async def push_candidates(self, candidates: list[Candidate]) -> None: ...

    def retire(self, seed_url_key: str) -> None:
        """Drop a seed's queued work and refuse new work; in-flight tasks continue."""
        ...

    def is_retired(self, seed_url_key: str) -> bool: ...
    async def take_for_ranking(self, n: int) -> list[Candidate]: ...
    def finish_ranking(self, n: int) -> None: ...

    @property
    def cooling(self) -> int:
        """Scored items a cooldown will release on its own."""
        ...

    @property
    def scoring(self) -> int:
        """Candidates out being scored, in neither half but still work."""
        ...

    @property
    def waiting(self) -> Buffer: ...

    @property
    def waiting_size(self) -> int: ...

    # the scored half.
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


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class GatedFrontier:
    """Own both candidate queues, deduplication, domain gates and snapshots."""

    def __init__(
        self,
        domain_budget: int = 50,
        aging_window: float = 600.0,
        age_factor: float = 1.0,
        source: PriorityQueue | None = None,
        buffer: Buffer | None = None,
    ) -> None:
        # Zero disables the per-domain page ceiling.
        self._domain_budget = domain_budget
        # Use the buffer contract independently of its scheduling strategy.
        self._waiting: Buffer = buffer if buffer is not None else RoundRobinBuffer()
        # Candidates out being scored: in neither half, still work.
        self._scoring = 0
        # Count domain refusals separately from natural frontier exhaustion.
        self.blocked_by_domain_budget = 0
        self._lock = asyncio.Lock()
        self._source: PriorityQueue = source or PriorityQueue(
            aging_window=aging_window,
            age_factor=age_factor,
        )
        # Retired seeds cannot add further work.
        self._retired: set[str] = set()
        self._visited: set[str] = set()
        self._domain_counters: dict[str, int] = {}
        self._global_counter: int = 0

    def retire(self, seed_url_key: str) -> None:
        """Drop a seed's queued work and refuse new work; in-flight tasks continue."""
        self._retired.add(seed_url_key)
        self._waiting.retire(seed_url_key)
        self._source.discard_seed(seed_url_key)

    def is_retired(self, seed_url_key: str) -> bool:
        return seed_url_key in self._retired

    # the unscored half ------------------------------------------------

    async def push_candidates(self, candidates: list[Candidate]) -> None:
        """Buffer candidates that have not been visited, queued or retired."""
        fresh = [c for c in candidates if not self.holds(c.url.url_key)]
        await self._waiting.add(fresh)

    async def take_for_ranking(self, n: int) -> list[Candidate]:
        """Take a batch from the buffer and account for ranking in progress."""
        batch = list(await self._waiting.drain(n))
        self._scoring += len(batch)
        return batch

    def finish_ranking(self, n: int) -> None:
        """Report that *n* candidates came back from scoring, or died there."""
        self._scoring = max(0, self._scoring - n)

    @property
    def scoring(self) -> int:
        return self._scoring

    @property
    def cooling(self) -> int:
        """Scored items waiting out a cooldown rather than a decision."""
        return self._source.cooling

    @property
    def waiting(self) -> Buffer:
        """The unscored half, for the rank pump's own wake-up signal."""
        return self._waiting

    @property
    def waiting_size(self) -> int:
        return int(self._waiting.size)

    # the scored half ---------------------------------------------------

    async def push_batch(self, items: list[FrontierItem]) -> None:
        async with self._lock:
            # Candidates leave the buffer before ranking; dedup against visited and queued items here.
            fresh = [i for i in items if i.url_key not in self._visited and not self._source.contains(i.url_key)]
            await self._source.add(fresh)

    def holds(self, url_key: str) -> bool:
        """Whether this URL is already spoken for, anywhere."""
        return url_key in self._visited or self._source.contains(url_key) or self._waiting.contains(url_key)

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
            if item.seed_url_key and item.seed_url_key in self._retired:
                return Gate.DROP
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
        """Serialize queues and frontier state through their public snapshot methods."""
        return FrontierSnapshot(
            task_id=task_id,
            ordering=self._source.dump(),
            waiting=self._waiting.dump(),
            visited=self._visited.copy(),
            budgets={"domain": dict(self._domain_counters), "global": self._global_counter},
        )

    def restore(self, snap: FrontierSnapshot) -> None:
        self._visited = snap.visited.copy()
        self._domain_counters = dict(snap.budgets.get("domain", {}))
        self._global_counter = snap.budgets.get("global", 0)
        if snap.waiting:
            self._waiting.load(snap.waiting)
        if snap.ordering:
            self._source.load(snap.ordering)
            return
        # A checkpoint written before orderings carried their own state.
        self._source.load(
            {
                "heap": [i.model_dump(mode="json") for i in snap.heap],
                "pending": [i.model_dump(mode="json") for i in snap.pending],
                "seq": snap.counters.get("seq", 0),
            }
        )
