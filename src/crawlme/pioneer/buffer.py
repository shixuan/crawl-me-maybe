"""Candidate buffer — in-memory staging area between link extraction and ranking.

Links extracted from each page become Candidates and accumulate here.  The
scheduler calls ready() to decide when to flush; when ready, drain(n) hands
candidates to the PreFilter → Ranker chain.

Key behaviours:

  1. Eviction    — when full, the lowest-quality candidate (by depth +
                   position heuristic) is evicted to make room.
  2. Dedup       — url_key checked against _seen; duplicates silently
                   dropped.  _seen persists across drain().
  3. Backpressure— add() never blocks; over-full is handled by eviction.
                   wait_until() provides asyncio.Condition-based blocking
                   for the rank loop to idle until ready() is true.

ready() triggers:  size >= 100  |  non-empty > 30s  |  frontier hungry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Protocol

from crawlme.schemas import Candidate

logger = logging.getLogger(__name__)


class Buffer(Protocol):
    """Contract for the in-memory candidate staging area."""

    @property
    def size(self) -> int: ...
    @property
    def is_empty(self) -> bool: ...

    async def add(self, candidates: list[Candidate]) -> None: ...
    async def drain(self, n: int | None = None) -> list[Candidate]: ...

    def ready(self, frontier_hungry: bool = False) -> bool: ...

    async def wait_until(self, predicate: Callable[[], bool] | None = None) -> None: ...
    async def wake(self) -> None: ...


class InMemoryBuffer:
    def __init__(self, capacity: int = 2000) -> None:
        self._capacity = capacity
        self._candidates: list[Candidate] = []
        self._seen: set[str] = set()
        self._cond = asyncio.Condition()
        self._last_added_at: float = 0.0

    # -- write path -------------------------------------------------------

    async def add(self, candidates: list[Candidate]) -> None:
        """Add a batch of candidates.  Evicts low-quality ones when full."""
        async with self._cond:
            for c in candidates:
                if c.url.url_key in self._seen:
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

    # -- read / drain path ------------------------------------------------

    async def drain(self, n: int | None = None) -> list[Candidate]:
        """Remove and return up to *n* candidates (all if None)."""
        async with self._cond:
            if n is None or n >= len(self._candidates):
                batch = self._candidates[:]
                self._candidates.clear()
            else:
                batch = self._candidates[:n]
                self._candidates = self._candidates[n:]
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
        """Notify waiters — used to unblock the rank pump on shutdown."""
        async with self._cond:
            self._cond.notify_all()

    # -- properties -------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self._candidates)

    @property
    def is_empty(self) -> bool:
        return len(self._candidates) == 0

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    # -- internal ---------------------------------------------------------

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
