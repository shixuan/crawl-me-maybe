"""The contracts this package is built on.

Kept apart from the classes that satisfy them, the way the fetcher and
feed packages do it: a frontier should not have to import a heap to say
what a frontier is, and swapping either implementation should not mean
editing the file that defines the other.
"""

from __future__ import annotations

import datetime
import enum
from collections.abc import Callable
from typing import Any, Protocol

from crawlme.pioneer.frontier.prefilter import PreFilterContext
from crawlme.schemas import Candidate, FrontierItem, FrontierItemStatus, FrontierSnapshot


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


class Buffer(Protocol):
    """Contract for the in-memory candidate staging area."""

    @property
    def size(self) -> int: ...
    @property
    def is_empty(self) -> bool: ...

    async def add(self, candidates: list[Candidate]) -> None: ...

    def contains(self, url_key: str) -> bool:
        """Whether this URL is already waiting here.

        Part of the contract because the frontier asks one dedup
        question across both halves, and a half that cannot answer it
        leaves the gap that made the same page arrive twice.
        """
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


class Frontier(Protocol):
    """Contract for the priority-queue URL frontier."""

    @property
    def size(self) -> int: ...

    #: the unscored half: candidates waiting for someone to score them.
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

    #: the scored half.
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
