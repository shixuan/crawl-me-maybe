"""Ranker protocol: the single interface for all ranking strategies.

Contract (duck-typed, no inheritance required):

  1. rank_batch() takes a candidate batch and returns one RankDecision
     per input candidate (matched by candidate_id).
  2. Rejected candidates must still appear in the output with
     dropped=True, so every candidate that reaches a ranker leaves an
     audit trail.
  3. Non-dropped decisions are returned sorted by priority descending
     (convention, not enforced).
  4. aclose() releases stage-owned resources (connections, model
     clients); implementations without resources may no-op.

Sub-rankers ignore parameters they don't use.  HybridRanker chains
multiple Ranker implementations into a funnel (see hybrid.py).
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
