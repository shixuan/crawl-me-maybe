"""Ranker protocol: the single interface for all ranking strategies.

Duck-typed, no inheritance required.  Two things the signature cannot
say: a rejected candidate still comes back, with dropped=True, so every
candidate that reaches a ranker leaves an audit trail; and the kept
ones come back sorted by priority descending, by convention rather than
by enforcement.
"""

from __future__ import annotations

from typing import Any, Protocol

from crawlme.schemas import Candidate, CrawlGoal, RankDecision, RankHistorySummary


class Ranker(Protocol):
    """Contract for pluggable ranking strategies.

    Implementations receive a batch of candidates, the crawl goal, a
    summary of what has been seen so far, and optional per-page context
    so they can incorporate source-page signals (title match, position
    bias) into the scoring decision.
    """

    async def rank_batch(
        self,
        goal: CrawlGoal,
        candidates: list[Candidate],
        history: RankHistorySummary,
        page_contexts: dict[str, dict[str, Any]] | None = None,
    ) -> list[RankDecision]: ...

    async def aclose(self) -> None: ...
