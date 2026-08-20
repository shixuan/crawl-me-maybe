"""Candidate buffer: what gets scored next, and in what mix.

This is where the scarce thing is decided.  Scoring costs an LLM call
per batch, so whatever leaves here is what the crawl will ever have an
opinion about; anything still sitting here when the run ends was never
considered at all.

Which makes the order it hands candidates out in a coverage decision,
not a detail.  It used to be first-come-first-served, and a run over
five accounts read fifty-three posts from one of them and none from
three others: the first listing fetched filled the queue, and the run
ended before the ranker reached anyone else.  Taking a turn from each
seed instead costs nothing and is the whole fix -- fairness belongs
here, upstream of the ranker, because this is the gate that binds.

Ordering *after* the ranker is a different question with a different
answer: there the scarce thing is the page budget, and the right way to
spend it is the priority the ranker just produced.

Candidates that pass PreFilter accumulate here.  The scheduler calls ready()
to decide when to flush; when ready, drain(n) hands candidates to the Ranker.

Key behaviours:

  1. Eviction   : when full, the lowest-quality candidate (by depth +
                   position heuristic) is evicted to make room.
  2. Dedup      : url_key checked against _seen; duplicates silently
                   dropped.  _seen persists across drain().
  3. Backpressure: add() never blocks; over-full is handled by eviction.
                   wait_until() provides asyncio.Condition-based blocking
                   for the rank loop to idle until ready() is true.

ready() triggers:  size >= 100  |  non-empty > 30s  |  frontier hungry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from crawlme.schemas import Candidate

logger = logging.getLogger(__name__)


def _take_turns(candidates: list[Candidate], n: int, start: str = "") -> tuple[list[Candidate], str]:
    """Up to *n*, one from each seed in turn, oldest first within a seed.

    Returns what to hand out and which seed the next call should start
    from.  Carrying that across calls is the whole point: a batch is
    smaller than the seed list often enough to matter, and starting from
    the front every time would let the first `n` seeds take every turn
    and leave the rest exactly as starved as first-come-first-served
    did, only with a larger cartel.

    A seed that runs out stops being asked, so its unused turns go to
    whoever still has candidates rather than being reserved and wasted.
    """
    groups: dict[str, list[Candidate]] = {}
    for c in candidates:
        groups.setdefault(c.seed_url_key or c.source_url_key or "", []).append(c)

    keys = list(groups)
    offset = keys.index(start) if start in keys else 0
    out: list[Candidate] = []
    served = offset
    while len(out) < n:
        took = False
        for i in range(len(keys)):
            key = keys[(offset + i) % len(keys)]
            queue = groups[key]
            if not queue:
                continue
            out.append(queue.pop(0))
            served = (keys.index(key) + 1) % len(keys)
            took = True
            if len(out) >= n:
                break
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
        # Where the next drain resumes the rotation.  Without it every
        # drain restarts at the first seed, and any seed past the batch
        # size never gets a turn at all.
        self._next_seed: str = ""

    #: write path -------------------------------------------------------

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

    #: read / drain path ------------------------------------------------

    async def drain(self, n: int | None = None) -> list[Candidate]:
        """Remove and return up to *n* candidates, a turn from each seed.

        Within one seed the oldest goes first: among an account's own
        posts there is nothing yet to prefer, since none of them have
        been scored.
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

    #: properties -------------------------------------------------------

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

    #: internal ---------------------------------------------------------

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
