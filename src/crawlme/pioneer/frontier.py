"""Priority-queue Frontier.

The Frontier is the crawler's scheduling hub: it owns the set of URLs waiting
to be fetched and decides *which one goes next*.  It does NOT call AI and does
NOT know page content — it only manages state.

Data structures
---------------
_heap    — min-heap keyed on (-priority, seq, url_key).  Python heapq is a
           min-heap, so negating priority makes the highest-priority item
           sort first.  seq is a monotonic tie-breaker (earlier push wins).
_items   — url_key -> FrontierItem for heap-resident items only.  When an
           item is gated (moved to pending), it is REMOVED from _items so
           that drain_pending can re-add it cleanly.
_visited — url_keys of pages already fetched (or permanently failed).
_pending — items whose next_available_at hasn't arrived yet.  Periodically
           drained back into the heap.

Gating (two levels)
-------------------
Per-item gate: each FrontierItem has next_available_at.  This is set by
  retry-backoff (exponential delay after 429/503) and crawl-delay (minimum
  interval between requests to the same domain).  If now < next_available_at,
  the item is moved to _pending until the gate passes.

Per-domain gate: the optional next_allowed callback (typically backed by
  RobotsPolicy) answers "when is this domain next allowed to be fetched?".
  If the domain is still cooling, the item goes to pending with an updated
  next_available_at.

Budget enforcement
------------------
Domain budget: after domain_budget successful fetches from a domain, further
  items from that domain are skipped (popped and discarded).

Global budget: after global_budget fetches across all domains, pop_next
  returns None, signalling the scheduler to stop.

pop_next retry loop
-------------------
_try_pop scans the heap.  If the top item is gated it moves to pending and
continues scanning.  If the heap runs out, _drain_pending moves cooled-down
items from pending back into the heap.  If anything was drained, the loop
retries _try_pop — otherwise pop_next returns None (frontier exhausted).

Snapshot / restore
------------------
snapshot() serialises the heap, pending list, visited set, budget counters,
and the global seq number into a FrontierSnapshot.  restore() reconstructs
everything from that snapshot.  This is the foundation of pause/resume and
crash recovery."""

from __future__ import annotations

import asyncio
import datetime
import heapq
from collections.abc import Callable

from crawlme.schemas import FrontierItem, FrontierItemStatus, FrontierSnapshot


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


_SEQ = 0


def _next_seq() -> int:
    """Monotonic sequence number so FIFO-tie-breaking is stable."""
    global _SEQ
    _SEQ += 1
    return _SEQ


class Frontier:
    def __init__(
        self,
        domain_budget: int = 50,
        aging_window: float = 600.0,
    ) -> None:
        self._domain_budget = domain_budget
        self._aging_window = aging_window
        self._lock = asyncio.Lock()

        # Python heapq is a min-heap.  We want the *highest* priority item
        # to pop first, so the heap key is (-priority, seq, url_key).
        # seq acts as a tie-breaker: for equal priority, earlier push wins.
        self._heap: list[tuple[float, int, str]] = []
        # url_key -> FrontierItem for heap-resident items only.
        # Items moved to _pending are removed from _items so drain_pending
        # can re-add them without conflicts.
        self._items: dict[str, FrontierItem] = {}

        self._visited: set[str] = set()
        self._domain_counters: dict[str, int] = {}
        self._global_counter: int = 0
        # Items whose next_available_at hasn't arrived yet.
        self._pending: list[FrontierItem] = []

    async def push_batch(self, items: list[FrontierItem]) -> None:
        async with self._lock:
            for item in items:
                if item.url_key in self._visited or item.url_key in self._items:
                    continue
                item.status = "QUEUED"
                item.enqueued_at = _utcnow()
                item.seq = _next_seq()
                self._items[item.url_key] = item
                heapq.heappush(self._heap, (-item.priority, item.seq, item.url_key))

    async def pop_next(
        self,
        now: datetime.datetime | None = None,
        next_allowed: Callable[[str], datetime.datetime] | None = None,
        global_budget: int | None = None,
    ) -> FrontierItem | None:
        """Return the highest-priority item that can be fetched right now.

        The loop retries: _try_pop scans the heap; if it finds nothing but
        _drain_pending moved a cooled-down item back into the heap, we
        retry _try_pop instead of returning None immediately.
        """
        now = now or _utcnow()
        async with self._lock:
            while True:
                result = self._try_pop(now, next_allowed, global_budget)
                if result is not None:
                    return result
                if not self._drain_pending(now):
                    return None

    def _try_pop(
        self,
        now: datetime.datetime,
        next_allowed: Callable[[str], datetime.datetime] | None,
        global_budget: int | None,
    ) -> FrontierItem | None:
        """Scan the heap top.  Gate, budget-check, and return the first
        eligible item, or None if the heap is exhausted."""
        while self._heap:
            _, _, url_key = self._heap[0]
            item = self._items.get(url_key)
            if item is None:
                heapq.heappop(self._heap)
                continue

            # Per-item gate (retry backoff, crawl-delay).
            if item.next_available_at > now:
                heapq.heappop(self._heap)
                self._items.pop(item.url_key, None)
                self._pending.append(item)
                continue

            # Per-domain gate via external robots.txt / crawl-delay policy.
            if next_allowed is not None:
                allowed_at = next_allowed(item.reg_domain)
                if allowed_at > now:
                    heapq.heappop(self._heap)
                    self._items.pop(item.url_key, None)
                    item.next_available_at = allowed_at
                    self._pending.append(item)
                    continue

            used = self._domain_counters.get(item.reg_domain, 0)
            if used >= self._domain_budget:
                heapq.heappop(self._heap)
                continue

            if global_budget is not None and self._global_counter >= global_budget:
                return None

            heapq.heappop(self._heap)
            item.status = "IN_FLIGHT"
            return item
        return None

    def _drain_pending(self, now: datetime.datetime) -> bool:
        """Move gated items whose cooldown has passed back into the heap.

        Returns True if at least one item was re-queued, so the caller can
        retry _try_pop.
        """
        ready = [i for i in self._pending if i.next_available_at <= now]
        self._pending = [i for i in self._pending if i.next_available_at > now]
        for item in ready:
            if item.url_key not in self._visited and item.url_key not in self._items:
                item.seq = _next_seq()
                self._items[item.url_key] = item
                heapq.heappush(self._heap, (-item.priority, item.seq, item.url_key))
        return len(ready) > 0

    async def record_outcome(self, item: FrontierItem, status: FrontierItemStatus) -> None:
        async with self._lock:
            item.status = status
            self._visited.add(item.url_key)
            self._items.pop(item.url_key, None)
            if status == "COMPLETED":
                self._domain_counters[item.reg_domain] = self._domain_counters.get(item.reg_domain, 0) + 1
                self._global_counter += 1

    async def mark_visited(self, url_key: str) -> None:
        async with self._lock:
            self._visited.add(url_key)

    def contains(self, url_key: str) -> bool:
        return url_key in self._visited or url_key in self._items

    @property
    def size(self) -> int:
        return len(self._heap) + len(self._pending)

    def snapshot(self, task_id: str = "") -> FrontierSnapshot:
        # Only capture heap-resident items (not pending ones, which are
        # tracked separately).  Pending items are already out of _items
        # and stored in _pending.
        heap_items = [self._items[url_key] for _, _, url_key in self._heap if url_key in self._items]
        return FrontierSnapshot(
            task_id=task_id,
            heap=heap_items,
            pending=list(self._pending),
            visited=self._visited.copy(),
            budgets={
                "domain": dict(self._domain_counters),
                "global": self._global_counter,
            },
            counters={"seq": _SEQ},
        )

    def restore(self, snap: FrontierSnapshot) -> None:
        self._heap.clear()
        self._items.clear()
        self._visited = snap.visited.copy()
        self._domain_counters = dict(snap.budgets.get("domain", {}))
        self._global_counter = snap.budgets.get("global", 0)
        self._pending = list(snap.pending)
        global _SEQ
        _SEQ = snap.counters.get("seq", 0)
        for item in snap.heap:
            self._items[item.url_key] = item
            heapq.heappush(self._heap, (-item.priority, item.seq, item.url_key))
