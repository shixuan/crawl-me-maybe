"""The contract for the frontier, and the one thing that satisfies it.

The frontier is the crawler's scheduling hub: it owns every URL that has
been discovered and not yet read, and decides which one goes next.  It
does not call AI and does not know page content; it only manages state.

It holds that set in two halves, neither of which it implements itself:
candidates waiting to be scored go to a Buffer (buffer.py), and scored
ones waiting for a fetch slot to a PriorityQueue (queue.py).  What is
left here -- gating, budgets, dedup, checkpoints -- is the same whatever
the traversal, which is the point: a feed inherits all of it instead of
growing a second copy.  See docs/refactor.md G2.

The contract lives here beside its implementation because there is one
implementation.  A second Frontier is what would justify splitting them
into a package, and until then the split would only cost a reader a file
to open.

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
    """Everything discovered and not yet fetched, in its two states.

    A frontier is the set of URLs a crawl has found and not read, which
    is both compartments here: candidates waiting for a score, and
    scored candidates waiting for a fetch slot.  They stay apart because
    an unscored candidate has no priority to sort by, and they stay
    *here* because splitting them across two owners left the crawl with
    two answers to "do I already have this URL" and a moment between
    them where both said no.

    Owning both also means a checkpoint covers both.  When the waiting
    half lived outside, a run that stopped with eighty-seven candidates
    still unscored resumed knowing nothing about them.

    It coordinates and does not implement: the rotation belongs to the
    Buffer, the heap and its cooldowns to the PriorityQueue.
    """

    def __init__(
        self,
        domain_budget: int = 50,
        aging_window: float = 600.0,
        age_factor: float = 1.0,
        source: PriorityQueue | None = None,
        buffer: Buffer | None = None,
    ) -> None:
        # Zero means no per-domain ceiling.  One is right for a link
        # graph, where a single site can otherwise absorb the whole run;
        # it is wrong for a feed, where every candidate shares the
        # platform's domain and the ceiling becomes a hidden total that
        # quietly overrides the page budget.
        self._domain_budget = domain_budget
        # The unscored half.  Typed to the contract, not to the rotation:
        # this package has already had two answers to "which candidate
        # gets scored next" and will have others.
        self._waiting: Buffer = buffer if buffer is not None else RoundRobinBuffer()
        # Candidates out being scored: in neither half, still work.
        self._scoring = 0
        # How many candidates that ceiling turned away.  A frontier can
        # be empty because there was nothing left or because everything
        # left was refused, and a run that cannot tell the difference
        # reports the second as completion.
        self.blocked_by_domain_budget = 0
        self._lock = asyncio.Lock()
        self._source: PriorityQueue = source or PriorityQueue(
            aging_window=aging_window,
            age_factor=age_factor,
        )
        self._visited: set[str] = set()
        self._domain_counters: dict[str, int] = {}
        self._global_counter: int = 0

    # the unscored half ------------------------------------------------

    async def push_candidates(self, candidates: list[Candidate]) -> None:
        """Hold candidates until something scores them.

        One question, asked once, covering both halves and what has
        already been read.  When the halves had separate owners each
        kept its own answer, and a candidate on its way from one to the
        other was unknown to both.
        """
        fresh = [c for c in candidates if not self.holds(c.url.url_key)]
        await self._waiting.add(fresh)

    async def take_for_ranking(self, n: int) -> list[Candidate]:
        """Hand out the next candidates to score, a turn from each seed.

        Counted while they are gone.  Between leaving here and coming
        back scored they are in neither half, and a run that read the
        two halves as "nothing left" ended while its next batch was
        still being scored.
        """
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
            # Scored items come back from the ranker, which was handed
            # them from the waiting half, so they are no longer held
            # there: only the read set and the scored half can object.
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
        """Store the ordering's state without reading into it.

        Naming its keys here made the checkpoint a copy of one ordering's
        internals: the moment the ordering became a composition of
        others, `heap` was absent and every checkpoint saved an empty
        queue, silently, and a resume began with nothing to fetch.
        """
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
