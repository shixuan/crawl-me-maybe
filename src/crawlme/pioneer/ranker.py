"""Ranker protocol + HybridRanker.

The Ranker is responsible for scoring candidates and deciding which ones
deserve a place in the Frontier.  It does NOT mutate candidates: it returns
immutable RankDecision records.

HybridRanker implements a pluggable multi-stage scoring pipeline:

  v0.1  RuleScorer only (7-factor heuristic, zero LLM cost).
        Candidates scoring below _RULE_THRESHOLD are dropped; survivors are
        returned sorted by priority descending.

  v0.2  Two-stage: RuleScorer pre-filters with a relaxed threshold (0.25),
        then LLMScorer fine-ranks the top 30 in a single batched LLM call.
        LLM failures fall back to RuleScorer scores.

  v0.3  Three-stage: EmbeddingRanker inserted between RuleScorer and
        LLMScorer for semantic similarity scoring at zero LLM cost.

See docs/ranking.md for the full funnel design and factor weight rationale.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from crawlme.pioneer.rule_scorer import RuleScorer
from crawlme.schemas import Candidate, CrawlGoal, RankDecision, RankHistorySummary

logger = logging.getLogger(__name__)

# Candidates with rule_score below this threshold are dropped.
# v0.1: 0.35 (conservative: RuleScorer is the final decision maker).
# v0.2: 0.25 (relaxed: LLMScorer can correct RuleScorer's mistakes).
_RULE_THRESHOLD = 0.35

_WORD_RE = re.compile(r"\w+")


class Ranker(Protocol):
    """Protocol for pluggable ranking strategies.

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


class HybridRanker:
    """Pluggable multi-stage scoring pipeline.

    v0.1 (current):
      RuleScorer only.  Candidates are grouped by source page so each group
      is scored with the correct source-page title and link count.  Those
      scoring below _RULE_THRESHOLD are marked dropped; the rest are returned
      sorted by priority descending.

    v0.2 (planned):
      Two-stage: RuleScorer pre-filters (relaxed 0.25 threshold), then
      LLMScorer fine-ranks the top 30 candidates in a single batched call.
      LLM failures gracefully fall back to RuleScorer scores.

    v0.3 (planned):
      Three-stage: EmbeddingRanker inserted between RuleScorer and LLMScorer
      for zero-LLM semantic similarity scoring.
    """

    def __init__(self, scorer: RuleScorer | None = None) -> None:
        self._scorer = scorer or RuleScorer()

    async def rank_batch(
        self,
        goal: CrawlGoal,
        candidates: list[Candidate],
        history: RankHistorySummary,
        page_contexts: dict[str, dict[str, Any]] | None = None,
    ) -> list[RankDecision]:
        keywords = _extract_keywords(goal.prompt)
        domain_prior = _build_domain_prior(history)
        pc = page_contexts or {}
        logger.debug("rank_batch.start candidates=%d keywords=%s pages=%d", len(candidates), keywords[:10], len(pc))

        # Group candidates by source page so each group gets the correct
        # source_page_title and page_link_count for title_match + position factors.
        groups: dict[str, list[Candidate]] = {}
        for c in candidates:
            key = c.source_url_key or ""
            groups.setdefault(key, []).append(c)

        all_scored: list[RankDecision] = []
        for source_key, group in groups.items():
            ctx = pc.get(source_key, {})
            source_title = ctx.get("title", "")
            link_count = ctx.get("link_count", 0)
            scored = self._scorer.score_batch(
                group,
                goal_keywords=keywords,
                source_page_title=source_title,
                page_link_count=link_count,
                domain_prior=domain_prior,
            )
            all_scored.extend(scored)

        kept: list[RankDecision] = []
        for d in all_scored:
            if d.priority < _RULE_THRESHOLD:
                d.dropped = True
            else:
                kept.append(d)

        kept.sort(key=lambda d: d.priority, reverse=True)
        return kept


def _extract_keywords(prompt: str) -> list[str]:
    return list(dict.fromkeys(w.lower() for w in _WORD_RE.findall(prompt)))


def _build_domain_prior(history: RankHistorySummary) -> dict[str, float]:
    """Extract domain prior scores from history hub domains.

    v0.1: hub_domains is a plain list of domain names with no per-domain
    scores yet.  Each hub domain gets a moderate boost (0.75) over unseen
    domains (0.5), but not a free pass.

    v0.2: FeedbackStore will provide per-domain avg_relevance from the
    feedback table, giving this factor real statistical weight.
    """
    return {d: 0.75 for d in history.hub_domains if d}
