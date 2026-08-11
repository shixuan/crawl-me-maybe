"""Ranker protocol + HybridRanker.

M1: HybridRanker wraps RuleScorer with a ≥0.35 threshold.  No LLM.
M2: LLMRanker added as the second stage; RuleScorer narrows the field,
     LLMRanker does fine-grained ranking on the top 30.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from crawlme.pioneer.rule_scorer import RuleScorer
from crawlme.schemas import Candidate, CrawlGoal, RankDecision, RankHistorySummary

logger = logging.getLogger(__name__)

# Threshold from ranking.md §第1层: rule_score ≥ 0.35 advances to LLM.
_RULE_THRESHOLD = 0.35

_WORD_RE = re.compile(r"\w+")


class Ranker(Protocol):
    """Protocol for pluggable ranking strategies.

    page_contexts maps source_url_key → {title, link_count} so the
    scorer can use per-page signals (title match, position bias).
    """

    async def rank_batch(
        self,
        goal: CrawlGoal,
        candidates: list[Candidate],
        history: RankHistorySummary,
        page_contexts: dict[str, dict[str, Any]] | None = None,
    ) -> list[RankDecision]: ...


class HybridRanker:
    """Two-stage ranker.

    v0.1 path (no LLM):
      1. Candidates are grouped by source page url_key
      2. RuleScorer scores each group with its source page's title + link count
      3. Candidates with rule_score < 0.35 are dropped
      4. Survivors are returned sorted by priority descending
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

    hub_domains is list[str] in M1 — just domain names with no scores yet.
    Assign a moderate boost (0.75) so they get a lift over unseen domains
    (0.5), but not a free pass.  M2 will add per-domain avg_relevance from
    the feedback table.
    """
    return {d: 0.75 for d in history.hub_domains if d}
