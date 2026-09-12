"""Rotate unranked candidates between seeds before spending LLM tokens."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from crawlme.schemas import Candidate

logger = logging.getLogger(__name__)


class Buffer(Protocol):
    """Contract for the in-memory candidate staging area."""

    @property
    def size(self) -> int: ...
    @property
    def is_empty(self) -> bool: ...

    async def add(self, candidates: list[Candidate]) -> None: ...

    def retire(self, seed_url_key: str) -> None: ...

    def contains(self, url_key: str) -> bool:
        """Report whether the URL is waiting in this buffer."""
        ...

    def dump(self) -> dict[str, Any]:
        """State for a checkpoint.  A crawl that stops mid-scoring holds
        most of its work here, and a resume that cannot read it back
        starts over without knowing what it had found."""
        ...

    def load(self, state: dict[str, Any]) -> None: ...
    async def drain(self, n: int | None = None) -> list[Candidate]: ...

    def ready(self, frontier_hungry: bool = False) -> bool: ...

    async def wait_until(self, predicate: Callable[[], bool] | None = None) -> None: ...
    async def wake(self) -> None: ...


# The bucket every proposed seed shares, and how often it gets a turn.
# One pass in four leaves the user's own seeds most of the flow.
_EXT_KEY = "\x00ext"
_EXT_EVERY = 4


def _take_turns(candidates: list[Candidate], n: int, start: str = "") -> tuple[list[Candidate], str]:
    """Take up to n candidates across seeds, FIFO within each seed.

    Return the next seed position so rotation continues fairly across batches."""
    groups: dict[str, list[Candidate]] = {}
    for c in candidates:
        # They share one turn, so proposing more changes how deep each
        # is read, not what the user's own seeds get.
        key = _EXT_KEY if c.seed_ext else (c.seed_url_key or c.source_url_key or "")
        groups.setdefault(key, []).append(c)

    keys = list(groups)
    offset = keys.index(start) if start in keys else 0
    out: list[Candidate] = []
    served = offset
    # One pass in four, but only while the user's own still have
    # candidates. After that an unused slot is better spent than empty.
    user_left = any(k != _EXT_KEY and q for k, q in groups.items())
    rounds = 0
    while len(out) < n:
        took = False
        for i in range(len(keys)):
            key = keys[(offset + i) % len(keys)]
            if key == _EXT_KEY and user_left and rounds % _EXT_EVERY:
                continue
            queue = groups[key]
            if not queue:
                continue
            out.append(queue.pop(0))
            served = (keys.index(key) + 1) % len(keys)
            took = True
            if len(out) >= n:
                break
        rounds += 1
        user_left = any(k != _EXT_KEY and q for k, q in groups.items())
        if not took:
            break
    return out, (keys[served] if keys else "")


class RoundRobinBuffer:
    def __init__(self, capacity: int = 2000) -> None:
        self._capacity = capacity
        self._candidates: list[Candidate] = []
        self._seen: set[str] = set()
        self._cond = asyncio.Condition()
        self._last_added_at: float = 0.0
        # Keep rotation position across batches so later seeds receive turns.
        self._next_seed: str = ""
        # Retired seeds cannot add further work.
        self._retired: set[str] = set()

    def retire(self, seed_url_key: str) -> None:
        """Drop pending candidates for a seed and refuse future additions from it."""
        self._retired.add(seed_url_key)
        self._candidates = [c for c in self._candidates if c.seed_url_key != seed_url_key]

    # write path -------------------------------------------------------

    async def add(self, candidates: list[Candidate]) -> None:
        """Add a batch of candidates.  Evicts low-quality ones when full."""
        async with self._cond:
            for c in candidates:
                if c.url.url_key in self._seen or c.seed_url_key in self._retired:
                    continue
                c.status = "BUFFERED"
                if len(self._candidates) >= self._capacity:
                    worst = self._worst_index()
                    if _quality(c) > _quality(self._candidates[worst]):
                        logger.debug(
                            "buffer.evict evicted=%s replaced=%s", self._candidates[worst].url.url_key, c.url.url_key
                        )
                        self._candidates[worst] = c
                else:
                    self._candidates.append(c)
                self._seen.add(c.url.url_key)
            self._last_added_at = time.monotonic()
            self._cond.notify_all()

    # read / drain path ------------------------------------------------

    async def drain(self, n: int | None = None) -> list[Candidate]:
        """Remove up to n candidates, rotating between seeds when taking a partial batch.

        Taking the whole buffer preserves insertion order; within each seed, use FIFO.
        """
        async with self._cond:
            if n is None or n >= len(self._candidates):
                batch = self._candidates[:]
                self._candidates.clear()
                return batch
            batch, self._next_seed = _take_turns(self._candidates, n, self._next_seed)
            taken = {id(c) for c in batch}
            self._candidates = [c for c in self._candidates if id(c) not in taken]
            return batch

    def ready(self, frontier_hungry: bool = False) -> bool:
        """True when the buffer should be flushed for ranking."""
        if len(self._candidates) >= 100:
            return True
        if self._candidates and (time.monotonic() - self._last_added_at) > 30:
            return True
        if frontier_hungry and self._candidates:
            return True
        return False

    async def wait_until(self, predicate: Callable[[], bool] | None = None) -> None:
        """Block until *predicate* (default ready) becomes true."""
        async with self._cond:
            await self._cond.wait_for(predicate or self.ready)

    async def wake(self) -> None:
        """Notify waiters: used to unblock the rank pump on shutdown."""
        async with self._cond:
            self._cond.notify_all()

    # properties -------------------------------------------------------

    def contains(self, url_key: str) -> bool:
        return url_key in self._seen

    def dump(self) -> dict[str, Any]:
        return {
            "candidates": [c.model_dump(mode="json") for c in self._candidates],
            "seen": sorted(self._seen),
            "next_seed": self._next_seed,
        }

    def load(self, state: dict[str, Any]) -> None:
        self._candidates = [Candidate.model_validate(c) for c in state.get("candidates") or []]
        self._seen = set(state.get("seen") or [])
        self._next_seed = str(state.get("next_seed") or "")

    @property
    def size(self) -> int:
        return len(self._candidates)

    @property
    def is_empty(self) -> bool:
        return len(self._candidates) == 0

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    # internal ---------------------------------------------------------

    def _worst_index(self) -> int:
        worst = 0
        worst_q = _quality(self._candidates[0])
        for i, c in enumerate(self._candidates[1:], start=1):
            q = _quality(c)
            if q < worst_q:
                worst_q = q
                worst = i
        return worst


def _quality(c: Candidate) -> float:
    """Cheap quality proxy for eviction: shallow + early-position = better."""
    return -c.depth * 0.1 - c.position * 0.001
