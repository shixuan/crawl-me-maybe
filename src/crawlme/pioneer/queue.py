"""Priority queue with cooldown gates and aging for low-priority items.

Seed rotation belongs to the unranked buffer."""

from __future__ import annotations

import datetime
import enum
import heapq
import logging
from collections.abc import Callable
from typing import Any

from crawlme.schemas import FrontierItem

logger = logging.getLogger(__name__)


class Gate(enum.Enum):
    """Gate outcomes for taking, deferring or dropping an item, or stopping the scan."""

    TAKE = "take"
    DEFER = "defer"
    DROP = "drop"
    STOP = "stop"


# Called with (item, now) while the source scans its own ordering.
GateFn = Callable[[FrontierItem, datetime.datetime], Gate]


_SEQ = 0


def _next_seq() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


class PriorityQueue:
    """Best-first queue with lazy removal, cooldowns and periodic aging."""

    def __init__(self, *, aging_window: float = 600.0, age_factor: float = 1.0) -> None:
        self._aging_window = aging_window
        self._age_factor = age_factor
        self._heap: list[tuple[float, int, str]] = []
        self._items: dict[str, FrontierItem] = {}
        # Track queued, deferred and in-flight items separately from stale heap entries.
        self._pending: list[FrontierItem] = []
        self._taken: set[str] = set()

    @property
    def size(self) -> int:
        """Work still waiting: queued plus cooling down, never in flight."""
        return len(self._items) + len(self._pending)

    @property
    def cooling(self) -> int:
        """Count deferred items that may become available when time advances."""
        return len(self._pending)

    def contains(self, url_key: str) -> bool:
        """Spoken for: queued, cooling down, or being fetched right now.

        Dedup asks this before enqueuing, and an item in flight has to
        answer yes or the same page is read twice.
        """
        return url_key in self._items or url_key in self._taken or any(i.url_key == url_key for i in self._pending)

    def keys(self) -> set[str]:
        return set(self._items) | self._taken | {i.url_key for i in self._pending}

    def discard(self, url_key: str) -> None:
        """Forget an item, wherever it currently sits."""
        self._items.pop(url_key, None)
        self._taken.discard(url_key)
        self._pending = [i for i in self._pending if i.url_key != url_key]

    def discard_seed(self, seed_url_key: str) -> None:
        """Remove pending items belonging to a seed."""
        self._items = {k: i for k, i in self._items.items() if i.seed_url_key != seed_url_key}
        self._pending = [i for i in self._pending if i.seed_url_key != seed_url_key]

    async def add(self, items: list[FrontierItem]) -> None:
        for item in items:
            if item.url_key in self._items:
                continue
            item.status = "QUEUED"
            item.enqueued_at = _utcnow()
            item.seq = _next_seq()
            self._items[item.url_key] = item
            heapq.heappush(self._heap, (-item.priority, item.seq, item.url_key))

    async def take(self, now: datetime.datetime, gate: GateFn) -> FrontierItem | None:
        """Return the highest-priority item currently allowed by the gate.

        Rescan after releasing cooled-down items, but exclude items deferred in this
        call so a non-time-based deferral cannot cause an infinite loop.
        """
        deferred: set[str] = set()
        while True:
            found = self._scan(now, gate, deferred)
            if found is not None:
                return found
            if not self._drain_pending(now, skip=deferred):
                return None

    def _scan(self, now: datetime.datetime, gate: GateFn, deferred: set[str]) -> FrontierItem | None:
        while self._heap:
            _, _, url_key = self._heap[0]
            item = self._items.get(url_key)
            if item is None:
                heapq.heappop(self._heap)  # stale: its item left another way
                continue

            decision = gate(item, now)
            if decision is Gate.STOP:
                return None
            if decision is Gate.DEFER:
                heapq.heappop(self._heap)
                self._items.pop(item.url_key, None)
                self._pending.append(item)
                deferred.add(item.url_key)
                continue
            if decision is Gate.DROP:
                heapq.heappop(self._heap)
                self._items.pop(item.url_key, None)
                continue

            heapq.heappop(self._heap)
            self._items.pop(item.url_key, None)
            self._taken.add(item.url_key)
            item.priority = self._effective_priority(item, now)
            item.status = "IN_FLIGHT"
            return item
        return None

    def peek(self) -> FrontierItem | None:
        """Return the live heap top without checking gates or refreshing aging."""
        while self._heap:
            url_key = self._heap[0][2]
            item = self._items.get(url_key)
            if item is not None:
                return item
            heapq.heappop(self._heap)
        return self._pending[0] if self._pending else None

    def _drain_pending(self, now: datetime.datetime, skip: set[str] | None = None) -> bool:
        """Return cooled-down items to the heap, reporting whether any moved."""
        skip = skip or set()
        ready = [i for i in self._pending if i.next_available_at <= now and i.url_key not in skip]
        moved = {i.url_key for i in ready}
        self._pending = [i for i in self._pending if i.url_key not in moved]
        for item in ready:
            if item.url_key not in self._items:
                item.seq = _next_seq()
                self._items[item.url_key] = item
                # Do not compound aging across deferred scans; persist it only when taking the item.
                heapq.heappush(self._heap, (-self._effective_priority(item, now), item.seq, item.url_key))
        return len(ready) > 0

    def _effective_priority(self, item: FrontierItem, now: datetime.datetime) -> float:
        """Age waiting items upward so a low score cannot starve forever.

        effective = priority + age_factor * (now - enqueued_at) / aging_window
        """
        age_seconds = (now - item.enqueued_at).total_seconds()
        if age_seconds <= 0 or self._aging_window <= 0:
            return item.priority
        return item.priority + self._age_factor * age_seconds / self._aging_window

    def dump(self) -> dict[str, Any]:
        """Serialize ordering state, including deferred items."""
        heap_items = [self._items[k] for _, _, k in self._heap if k in self._items]
        return {
            "heap": [i.model_dump(mode="json") for i in heap_items],
            "pending": [i.model_dump(mode="json") for i in self._pending],
            "seq": _SEQ,
        }

    def load(self, state: dict[str, Any]) -> None:
        global _SEQ
        self._heap.clear()
        self._items.clear()
        self._taken.clear()
        self._pending = [_as_item(raw) for raw in state.get("pending", [])]
        _SEQ = state.get("seq", 0)
        for raw in state.get("heap", []):
            item = _as_item(raw)
            self._items[item.url_key] = item
            heapq.heappush(self._heap, (-item.priority, item.seq, item.url_key))


def _as_item(raw: Any) -> FrontierItem:
    """Accept either form: a checkpoint read back is data, not models."""
    return raw if isinstance(raw, FrontierItem) else FrontierItem.model_validate(raw)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
