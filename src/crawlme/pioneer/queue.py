"""The queue of pages waiting to be fetched, and when each may go.

A max-heap on the priority the ranker produced, plus the two things a
heap alone cannot express: an item held back until a rate limit passes,
and an item aged upward so that a low score cannot wait forever.

This was a swappable seam for a while, with a fair-rotation ordering
beside this one, on the theory that a run over several seeds wants a
turn from each.  It does -- but upstream, over which candidates get
scored at all, because an LLM call per batch is the scarce thing and
whatever never leaves the buffer is never considered.  By the time an
item reaches here it has been scored and the only question left is
which score goes first, so there is one structure here and no plug.

The Frontier used to be six things at once: ordering, per-item gating,
budget enforcement, dedup state, counters, and checkpointing.  Only the
first of those is specific to how a source is traversed.  One run wants the best candidate anywhere; another
wants a turn taken from each seed.  The other five are identical either way, and duplicating them per
traversal is how the two halves drift apart.

So an Ordering answers one question, "who is next", and the Frontier
shell keeps everything else.  The shell hands down a gate function
because only it knows about robots delays and budgets, and the source
calls it while scanning because only the source knows its own order.

See docs/refactor.md G2 and G6.
"""

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
    """What the shell decided about one candidate item.

    Four outcomes rather than a boolean, because the existing frontier
    already distinguishes them and collapsing any two would change
    behavior: a rate-limited item comes back later, a budget-exhausted
    domain never does, and a spent global budget ends the scan for
    everyone rather than for one item.
    """

    TAKE = "take"
    DEFER = "defer"
    DROP = "drop"
    STOP = "stop"


#: Called with (item, now) while the source scans its own ordering.
GateFn = Callable[[FrontierItem, datetime.datetime], Gate]


_SEQ = 0


def _next_seq() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


class PriorityQueue:
    """Best-first ordering over a link graph.

    Python's heapq is a min-heap and we want the highest priority first,
    so the key is (-priority, seq, url_key); seq breaks ties in push
    order.  Deferred items leave the heap entirely and live in a pending
    list until their cooldown passes, which is why they are also removed
    from the index: drain can then re-add them without a conflict.
    """

    def __init__(self, *, aging_window: float = 600.0, age_factor: float = 1.0) -> None:
        self._aging_window = aging_window
        self._age_factor = age_factor
        self._heap: list[tuple[float, int, str]] = []
        self._items: dict[str, FrontierItem] = {}
        # Three sets, one meaning each, so no count has to be inferred:
        # queued and in the heap, cooling down, and handed out but not
        # settled.  The heap keeps stale entries either way -- it cannot
        # delete from the middle -- but nothing reads the heap to answer
        # a question about membership or size.
        self._pending: list[FrontierItem] = []
        self._taken: set[str] = set()

    @property
    def size(self) -> int:
        """Work still waiting: queued plus cooling down, never in flight."""
        return len(self._items) + len(self._pending)

    @property
    def cooling(self) -> int:
        """Items held back by the clock, which time alone will release.

        Distinct from items a gate refuses outright: a spent budget
        refuses the same item forever, so a caller that waits for one of
        those to become available waits for nothing.
        """
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
        """Highest-priority item the gate allows right now.

        Retries: a scan that finds nothing may still have cooled-down
        items waiting, so draining them and rescanning beats returning
        None while work is available.

        Anything deferred during this call is held back from the retry.
        Without that, a gate that defers for a reason other than the clock
        livelocks: the item is already due, so drain returns it at once,
        so the scan defers it again, forever.
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
        """The heap's top, with dead entries cleared off it on the way.

        Reports the stored priority rather than the aged one: aging is
        applied when an item is taken, and recomputing it here for every
        look would make a read cost as much as a write.
        """
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
                item.priority = self._effective_priority(item, now)
                self._items[item.url_key] = item
                heapq.heappush(self._heap, (-item.priority, item.seq, item.url_key))
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
        """State as plain data, the same shape in memory and on disk.

        Returning models worked until a checkpoint was written and read
        back, at which point load() was handed dicts and reached for an
        attribute they do not have.
        """
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
