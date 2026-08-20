"""What comes next, separated from whether it may go now.

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
from typing import Any, Protocol

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


class Ordering(Protocol):
    """Ordering, and nothing else."""

    @property
    def size(self) -> int: ...

    def contains(self, url_key: str) -> bool: ...

    def keys(self) -> set[str]: ...

    async def add(self, items: list[FrontierItem]) -> None: ...

    async def take(self, now: datetime.datetime, gate: GateFn) -> FrontierItem | None: ...

    def peek(self) -> FrontierItem | None:
        """What take() would return next, without consuming or gating.

        Exists so one ordering can be nested inside another: the outer
        one has to know how good a group's next item is to order the
        groups, and asking by taking would consume it.
        """
        ...

    def discard(self, url_key: str) -> None: ...

    def dump(self) -> dict[str, Any]: ...

    def load(self, state: dict[str, Any]) -> None: ...


_SEQ = 0


def _next_seq() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


class BestFirst:
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


class RoundRobin:
    """Take a turn from each, in the order they first appeared.

    Priority is ignored on purpose: this exists for the question "what do
    these have", where the loudest source outranking every other one is
    the problem rather than the answer.

    Deferred items keep their place, so a source waiting on a rate limit
    resumes its turn instead of losing it.
    """

    def __init__(self) -> None:
        self._items: dict[str, FrontierItem] = {}
        self._order: list[str] = []
        self._cursor = 0

    @property
    def size(self) -> int:
        return len(self._items)

    def contains(self, url_key: str) -> bool:
        return url_key in self._items

    def keys(self) -> set[str]:
        return set(self._items)

    def discard(self, url_key: str) -> None:
        # Out of the rotation as well as the store: leaving the key
        # behind lets a later add append a second copy of it, and the
        # rotation then visits that group twice per lap.
        if self._items.pop(url_key, None) is not None:
            self._order = [k for k in self._order if k != url_key]

    def peek(self) -> FrontierItem | None:
        for key in self._live_keys():
            return self._items[key]
        return None

    async def add(self, items: list[FrontierItem]) -> None:
        for item in items:
            if item.url_key in self._items:
                continue
            item.status = "QUEUED"
            item.enqueued_at = _utcnow()
            item.seq = _next_seq()
            self._items[item.url_key] = item
            self._order.append(item.url_key)

    async def take(self, now: datetime.datetime, gate: GateFn) -> FrontierItem | None:
        keys = self._live_keys()
        for offset in range(len(keys)):
            key = keys[(self._cursor + offset) % len(keys)]
            item = self._items.get(key)
            if item is None:
                continue
            decision = gate(item, now)
            if decision is Gate.STOP:
                return None
            if decision is Gate.DEFER:
                continue
            if decision is Gate.DROP:
                self.discard(key)
                continue
            self.discard(key)
            item.status = "IN_FLIGHT"
            # Not offset + 1: taking removes this key, so the next
            # position already holds what came after it.  Advancing past
            # it as well skips a turn, which on a two-source rotation
            # means one of them never gets one.
            self._cursor = (self._cursor + offset) % max(len(keys) - 1, 1)
            return item
        return None

    def _live_keys(self) -> list[str]:
        """Insertion order, with anything already taken forgotten."""
        self._order = [k for k in self._order if k in self._items]
        return self._order

    def dump(self) -> dict[str, Any]:
        return {
            "items": [i.model_dump(mode="json") for i in self._items.values()],
            "order": list(self._order),
            "cursor": self._cursor,
        }

    def load(self, state: dict[str, Any]) -> None:
        self._items = {}
        for raw in state.get("items") or []:
            item = _as_item(raw)
            self._items[item.url_key] = item
        self._order = [k for k in (state.get("order") or []) if k in self._items]
        self._cursor = int(state.get("cursor") or 0)


#: Prefix for the stand-in items an outer ordering ranks.  Real items
#: never reach it, so an outer ordering can be any Ordering at all
#: without its bookkeeping colliding with a group's own.
_GROUP_PREFIX = "group:"


class HybridOrdering:
    """Two orderings, one for the groups and one inside each group.

    Implements no ordering of its own. It partitions items, gives each
    partition its own ordering, and lets a second ordering decide whose
    turn it is -- so an algorithm written once can serve either level,
    and a new one plugs into either without touching this.

    The outer ordering ranks a stand-in item per group whose priority is
    that group's next item's. Two consequences follow, and the second is
    the reason this shape was worth the bookkeeping:

      outer=RoundRobin -> a turn from each group.
      outer=BestFirst  -> the best group's best item, which *is* the
                          best item anywhere. Global best-first is not a
                          separate code path, it is this with a
                          different plug.

    Gating stays inside the groups. The outer only names a group; the
    group's own take() applies the gate, defers, drops, and keeps the
    rule that stops a deferring gate from livelocking. None of that is
    reimplemented here, and none of it costs anything extra.
    """

    def __init__(
        self,
        partition_of: Callable[[FrontierItem], str],
        outer: Ordering,
        make_inner: Callable[[], Ordering],
    ) -> None:
        self._partition_of = partition_of
        self._outer = outer
        self._make_inner = make_inner
        self._groups: dict[str, Ordering] = {}
        self._group_of: dict[str, str] = {}

    @property
    def size(self) -> int:
        return sum(g.size for g in self._groups.values())

    def contains(self, url_key: str) -> bool:
        """Queued or in flight, which is what dedup means by "already have"."""
        return url_key in self._group_of

    def keys(self) -> set[str]:
        return set(self._group_of)

    def discard(self, url_key: str) -> None:
        name = self._group_of.pop(url_key, None)
        if name is not None:
            self._groups[name].discard(url_key)

    def peek(self) -> FrontierItem | None:
        token = self._outer.peek()
        return self._groups[_group_name(token)].peek() if token is not None else None

    async def add(self, items: list[FrontierItem]) -> None:
        # Order-preserving: which group registers first decides where it
        # sits in the rotation, and a set would make that vary run to run.
        touched: dict[str, None] = {}
        for item in items:
            if item.url_key in self._group_of:
                continue
            name = self._partition_of(item)
            if name not in self._groups:
                self._groups[name] = self._make_inner()
            self._group_of[item.url_key] = name
            await self._groups[name].add([item])
            touched[name] = None
        for name in touched:
            await self._refresh_token(name)

    async def take(self, now: datetime.datetime, gate: GateFn) -> FrontierItem | None:
        """One item, from whichever group the outer ordering names.

        A group that has nothing to give right now has its token set
        aside rather than dropped, so a rate-limited source keeps its
        place in the rotation instead of losing its turn.
        """
        held: list[FrontierItem] = []
        try:
            while True:
                token = await self._outer.take(now, _ungated)
                if token is None:
                    return None
                name = _group_name(token)
                item = await self._groups[name].take(now, gate)
                if item is not None:
                    # The key stays. An item handed out is in flight, not
                    # gone, and dedup asks contains() whether a URL is
                    # already spoken for: forgetting it here lets the same
                    # page be discovered again mid-fetch and queued twice.
                    # record_outcome discards it when the fetch settles.
                    await self._refresh_token(name)
                    return item
                held.append(token)
        finally:
            if held:
                await self._outer.add(held)

    async def _refresh_token(self, name: str) -> None:
        """Restate what this group is worth, or withdraw it if it is spent."""
        key = f"{_GROUP_PREFIX}{name}"
        self._outer.discard(key)
        nxt = self._groups[name].peek()
        if nxt is None:
            return
        await self._outer.add(
            [
                FrontierItem(
                    url=nxt.url,
                    url_key=key,
                    priority=nxt.priority,
                    score_source="group",
                    reg_domain=nxt.reg_domain,
                )
            ]
        )

    def dump(self) -> dict[str, Any]:
        return {
            "outer": self._outer.dump(),
            "groups": {name: g.dump() for name, g in self._groups.items()},
        }

    def load(self, state: dict[str, Any]) -> None:
        self._groups.clear()
        self._group_of.clear()
        self._outer.load(state.get("outer") or {})
        for name, sub in (state.get("groups") or {}).items():
            inner = self._make_inner()
            inner.load(sub)
            self._groups[name] = inner
            for key in inner.keys():
                self._group_of[key] = name


def _group_name(token: FrontierItem) -> str:
    return token.url_key[len(_GROUP_PREFIX) :]


def _ungated(_item: FrontierItem, _now: datetime.datetime) -> Gate:
    """The outer ordering ranks groups; only a group's own items are gated."""
    return Gate.TAKE
