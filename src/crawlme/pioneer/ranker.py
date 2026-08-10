"""Ranker protocol + HybridRanker.

M1: HybridRanker wraps RuleScorer with a ≥0.35 threshold.  No LLM.
M2: LLMRanker added as the second stage; RuleScorer narrows the field,
     LLMRanker does fine-grained ranking on the top 30.
"""

from __future__ import annotations

import re
from typing import Protocol

from crawlme.pioneer.rule_scorer import RuleScorer
from crawlme.schemas import Candidate, CrawlGoal, RankDecision, RankHistorySummary

# Threshold from ranking.md §第1层: rule_score ≥ 0.35 advances to LLM.
_RULE_THRESHOLD = 0.35

_WORD_RE = re.compile(r"\w+")


class Ranker(Protocol):
    """Protocol for pluggable ranking strategies."""

    async def rank_batch(
        self,
        goal: CrawlGoal,
        candidates: list[Candidate],
        history: RankHistorySummary,
    ) -> list[RankDecision]: ...


class HybridRanker:
    """Two-stage ranker.

    v0.1 path (no LLM):
      1. RuleScorer scores every candidate
      2. Candidates with rule_score < 0.35 are dropped
      3. Survivors are returned sorted by priority descending
    """

    def __init__(self, scorer: RuleScorer | None = None) -> None:
        self._scorer = scorer or RuleScorer()

    async def rank_batch(
        self,
        goal: CrawlGoal,
        candidates: list[Candidate],
        history: RankHistorySummary,
    ) -> list[RankDecision]:
        # history is unused in M1 — accepted for protocol compatibility.
        _ = history

        keywords = _extract_keywords(goal.prompt)
        domain_prior = _build_domain_prior(history)

        scored = self._scorer.score_batch(candidates, goal_keywords=keywords, domain_prior=domain_prior)

        kept: list[RankDecision] = []
        for d in scored:
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

    hub_domains is list[str] in M1 — just domain names with no scores yet.
    Assign a moderate boost (0.75) so they get a lift over unseen domains
    (0.5), but not a free pass.  M2 will add per-domain avg_relevance from
    the feedback table.
    """
    return {d: 0.75 for d in history.hub_domains if d}
