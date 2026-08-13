"""HybridRanker: orchestrates a pluggable multi-stage ranking pipeline.

Chains Ranker implementations into a funnel: each stage only sees the
candidates the previous stage let through, so costs shrink along with
the candidate set.

  v0.1  RuleRanker only (7-factor heuristic, zero LLM cost).

  v0.1.1
        RuleRanker (ordering only) -> EmbeddingRanker (semantic
        top-K selection).  The embedding stage is opt-in: without an
        EMBEDDING_MODEL configured, behavior is unchanged from v0.1.

  v0.2  RuleRanker pre-filters with a relaxed threshold (0.25), then
        LLMRanker fine-ranks the top 30 in a single batched LLM call.
        LLM failures fall back to earlier-stage scores.

  v0.3  Playwright fetch, Prompt Cache, user-feedback learning.

See docs/ranking.md for the full funnel design and factor weight rationale.
"""

from __future__ import annotations

import logging
from typing import Any

from crawlme.pioneer.ranker.base import Ranker
from crawlme.pioneer.ranker.rule import RuleRanker
from crawlme.schemas import Candidate, CrawlGoal, RankDecision, RankHistorySummary

logger = logging.getLogger(__name__)

# Weight of the embedding stage in the blended final priority.
# Tuned on the E5 sweep (benchmark/sweep_params.py): at keep=60 the
# 0.8/0.2 blend keeps the same survivor set as pure sim but orders it
# best (AP 0.994 vs 0.965).  More rule weight (0.6) drops a couple of
# noise items at the cost of AP; less (0.9+) sinks semantic_hard items
# to the bottom of the keep window.
_EMBEDDING_WEIGHT = 0.8


class HybridRanker:
    """Multi-stage ranking pipeline.

    By default only the rule stage is wired (v0.1).  Pass *embedding*
    and *llm* rankers to enable the v0.2 / v0.3 stages.
    """

    def __init__(
        self,
        rule: Ranker | None = None,
        embedding: Ranker | None = None,
        llm: Ranker | None = None,
    ) -> None:
        self._rule = rule or RuleRanker()
        self._embedding = embedding
        self._llm = llm

    async def rank_batch(
        self,
        goal: CrawlGoal,
        candidates: list[Candidate],
        history: RankHistorySummary,
        page_contexts: dict[str, dict[str, Any]] | None = None,
    ) -> list[RankDecision]:
        """Run the funnel: rule -> embedding -> llm.

        Each stage returns one decision per candidate it saw.  Later
        stages overwrite earlier decisions by candidate_id; candidates
        dropped by an earlier stage never reach later ones.

        A failing embedding stage falls back to the rule stage's
        decisions: a dead embedding API never blocks the pipeline.
        """
        decisions = await self._rule.rank_batch(goal, candidates, history, page_contexts)
        survivors = _survive(candidates, decisions)

        if self._embedding is not None:
            try:
                emb = await self._embedding.rank_batch(goal, survivors, history, page_contexts)
            except Exception:
                logger.warning(
                    "rank.embedding_failed candidates=%d: falling back to rule scores",
                    len(survivors),
                    exc_info=True,
                )
                emb = None
            if emb is not None:
                decisions = _blend(decisions, emb, _EMBEDDING_WEIGHT)
                survivors = _survive(survivors, emb)

        if self._llm is not None:
            llm = await self._llm.rank_batch(goal, survivors, history, page_contexts)
            decisions = _merge(decisions, llm)

        return decisions


def _survive(candidates: list[Candidate], decisions: list[RankDecision]) -> list[Candidate]:
    """Candidates whose decision is not dropped."""
    dropped_by_id = {d.candidate_id: d.dropped for d in decisions}
    return [c for c in candidates if not dropped_by_id.get(c.candidate_id, False)]


def _merge(prev: list[RankDecision], new: list[RankDecision]) -> list[RankDecision]:
    """Overlay *new* decisions onto *prev* by candidate_id."""
    by_id = {d.candidate_id: d for d in prev}
    by_id.update({d.candidate_id: d for d in new})
    return list(by_id.values())


def _blend(prev: list[RankDecision], new: list[RankDecision], weight: float) -> list[RankDecision]:
    """Blend *new* stage priorities with *prev* stage by candidate_id.

    final = weight * new + (1 - weight) * prev.  The dropped flag and
    ranker tag follow *new*; the rule score is appended to the
    rationale for auditability.
    """
    by_id = {d.candidate_id: d for d in prev}
    for d in new:
        old = by_id.get(d.candidate_id)
        if old is not None:
            d.priority = round(weight * d.priority + (1 - weight) * old.priority, 4)
            if d.rationale and old.rationale:
                d.rationale = f"{d.rationale} rule_score={old.priority:.4f}"
        by_id[d.candidate_id] = d
    return list(by_id.values())
