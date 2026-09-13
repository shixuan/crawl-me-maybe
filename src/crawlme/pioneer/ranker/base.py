"""Ranking contract for candidate batches."""

from __future__ import annotations

from typing import Any, Protocol

from crawlme.schemas import Candidate, CrawlGoal, RankDecision, RankHistorySummary


class Ranker(Protocol):
    """Score candidate batches using the goal, history and source-page context."""

    async def rank_batch(
        self,
        goal: CrawlGoal,
        candidates: list[Candidate],
        history: RankHistorySummary,
        page_contexts: dict[str, dict[str, Any]] | None = None,
    ) -> list[RankDecision]: ...

    async def aclose(self) -> None: ...
