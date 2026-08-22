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
#
# This only decides anything when no LLM stage follows, because the LLM
# stage overwrites rather than blends, and it should: measured against
# the same analyzer labels, it orders at AP 0.936 where the embedding
# stage reaches 0.556 and the rule stage 0.450.  Nor does it decide who
# survives -- the embedding stage picks its top-K on cosine alone,
# before any blending.
#
# The 0.8 came from a sweep whose script no longer exists, over a set of
# candidates something else had already kept.  Its headline number was
# AP 0.994, which is what a survivor set looks like rather than what a
# ranker is worth, so read the value as untested rather than tuned.
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
        decisions; a failing LLM stage keeps the earlier stages' scores.
        A dead provider never blocks the pipeline.
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
            try:
                llm = await self._llm.rank_batch(goal, survivors, history, page_contexts)
            except Exception:
                logger.warning(
                    "rank.llm_failed candidates=%d: keeping earlier-stage scores",
                    len(survivors),
                    exc_info=True,
                )
                llm = None
            if llm is not None:
                decisions = _merge(decisions, llm)

        return decisions

    async def aclose(self) -> None:
        """Release stage-held resources (the embedding vector cache).

        The rule stage holds nothing, so only the later stages close.
        """
        for stage in (self._embedding, self._llm):
            if stage is not None:
                await stage.aclose()


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
