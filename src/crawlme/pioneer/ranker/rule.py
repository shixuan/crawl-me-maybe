"""RuleRanker: 7-factor heuristic scoring, zero LLM cost.

v0.1's only ranker.  Computes a weighted-average score in [0, 1] per
candidate and applies a hard threshold: candidates below it are marked
dropped, survivors are returned sorted by priority descending.

Formula:  score = sum(weight_i * factor_i) / sum(weight_i)

  1. Anchor text match       (w=0.30) : Jaccard(anchor words, goal keywords)
  2. Surrounding text match  (w=0.15) : Jaccard(snippet words, goal keywords)
  3. Source page title match (w=0.15) : Jaccard(title words, goal keywords)
  4. Domain prior            (w=0.15) : avg_relevance from cross-task history
  5. Path depth penalty      (w=0.10) : 1 / sqrt(depth + 1)
  6. URL path signal         (w=0.10) : about/contact/privacy -> 0,
                                        docs/blog/news -> 1, default -> 0.5
  7. Position signal         (w=0.05) : 1 - (position / page_link_count)

When goal_keywords is missing, factors 1-3 default to 0.5 (neutral).
Domain prior defaults to 0.5 for unseen domains.

See docs/ranking.md for the funnel design and factor weight rationale.
"""

from __future__ import annotations

import datetime
import math
import re
from typing import Any
from urllib.parse import urlparse

from crawlme.schemas import Candidate, CrawlGoal, RankDecision, RankHistorySummary

# URL paths that signal low-value pages.
_NEGATIVE_PATH_TOKENS = frozenset(
    {
        "about",
        "contact",
        "privacy",
        "terms",
        "login",
        "signup",
        "signin",
        "register",
        "cart",
        "checkout",
        "account",
        "subscribe",
        "unsubscribe",
    }
)

# URL paths that signal content-rich pages.
_POSITIVE_PATH_TOKENS = frozenset(
    {
        "docs",
        "documentation",
        "blog",
        "news",
        "article",
        "post",
        "guide",
        "tutorial",
        "reference",
        "api",
        "spec",
        "changelog",
        "release",
    }
)

_WORD_RE = re.compile(r"\w+")


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class RuleRanker:
    """Heuristic scoring with a hard threshold.

    v0.1: threshold 0.35 (conservative: RuleRanker is the final decision
    maker).  v0.2: 0.25 (relaxed: later stages can correct its mistakes).
    """

    def __init__(self, threshold: float = 0.35) -> None:
        self._threshold = threshold

    async def rank_batch(
        self,
        goal: CrawlGoal,
        candidates: list[Candidate],
        history: RankHistorySummary,
        page_contexts: dict[str, dict[str, Any]] | None = None,
    ) -> list[RankDecision]:
        """Score all candidates, mark below-threshold ones dropped.

        Candidates are grouped by source page so each group is scored
        with the correct source-page title and link count (factors 3
        and 7).  Returns one decision per input candidate: survivors
        first (priority descending), then dropped ones.
        """
        # LLM-curated keywords when the Goal Enhancer ran; bare
        # tokenization otherwise.
        keywords = list(goal.keywords) if goal.keywords else _extract_keywords(goal.prompt)
        domain_prior = _build_domain_prior(history)
        pc = page_contexts or {}

        groups: dict[str, list[Candidate]] = {}
        for c in candidates:
            key = c.source_url_key or ""
            groups.setdefault(key, []).append(c)

        kept: list[RankDecision] = []
        dropped: list[RankDecision] = []
        for source_key, group in groups.items():
            ctx = pc.get(source_key, {})
            scored = self.score_batch(
                group,
                goal_keywords=keywords,
                source_page_title=ctx.get("title", ""),
                page_link_count=ctx.get("link_count", 0),
                domain_prior=domain_prior,
            )
            for d in scored:
                if d.priority < self._threshold:
                    d.dropped = True
                    dropped.append(d)
                else:
                    kept.append(d)

        kept.sort(key=lambda d: d.priority, reverse=True)
        return kept + dropped

    def score_batch(
        self,
        candidates: list[Candidate],
        *,
        goal_keywords: list[str] | None = None,
        source_page_title: str = "",
        page_link_count: int = 0,
        domain_prior: dict[str, float] | None = None,
    ) -> list[RankDecision]:
        """Pure scoring without thresholding or ordering.

        Public so factor-level behavior is directly testable.
        """
        gk = goal_keywords or []
        dp = domain_prior or {}
        decisions: list[RankDecision] = []

        for c in candidates:
            priority, factors = _score_one(c, gk, source_page_title, page_link_count, dp)
            decisions.append(
                RankDecision(
                    candidate_id=c.candidate_id,
                    url_key=c.url.url_key,
                    priority=round(priority, 4),
                    dropped=False,
                    ranker="rule",
                    rationale=_format_rationale(priority, factors),
                    decided_at=_utcnow(),
                )
            )
        return decisions


#: factors -----------------------------------------------------------

_F1_W, _F2_W, _F3_W = 0.30, 0.15, 0.15
_F4_W, _F5_W, _F6_W, _F7_W = 0.15, 0.10, 0.10, 0.05
_TOTAL_W = _F1_W + _F2_W + _F3_W + _F4_W + _F5_W + _F6_W + _F7_W


def _score_one(
    c: Candidate,
    goal_keywords: list[str],
    source_page_title: str,
    page_link_count: int,
    domain_prior: dict[str, float],
) -> tuple[float, dict[str, float]]:
    gk = goal_keywords

    # 1. Anchor text match
    f1 = _jaccard(_words(c.anchor or ""), gk, c.anchor or "") if gk else 0.5

    # 2. Surrounding text match (snippet)
    f2 = _jaccard(_words(c.snippet or ""), gk, c.snippet or "") if gk else 0.5

    # 3. Source page title match
    f3 = _jaccard(_words(source_page_title), gk, source_page_title) if gk else 0.5

    # 4. Domain prior
    f4 = domain_prior.get(c.url.reg_domain, 0.5)

    # 5. Path depth penalty
    f5 = 1.0 / math.sqrt(c.depth + 1)

    # 6. URL path signal
    f6 = _path_signal(c.url.raw)

    # 7. Position signal
    if page_link_count > 0:
        f7 = 1.0 - (c.position / page_link_count)
    else:
        f7 = 0.5

    factors = {
        "anchor_match": round(f1, 4),
        "snippet_match": round(f2, 4),
        "title_match": round(f3, 4),
        "domain_prior": round(f4, 4),
        "depth": round(f5, 4),
        "path_signal": round(f6, 4),
        "position": round(f7, 4),
    }

    numerator = _F1_W * f1 + _F2_W * f2 + _F3_W * f3 + _F4_W * f4 + _F5_W * f5 + _F6_W * f6 + _F7_W * f7
    score = numerator / _TOTAL_W
    return score, factors


def _format_rationale(score: float, factors: dict[str, float]) -> str:
    parts = [f"rule_score={score:.4f}"]
    parts.extend(f"{k}={v:.3f}" for k, v in factors.items())
    return " ".join(parts)


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


#: helpers -----------------------------------------------------------


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text)}


def _jaccard(a: set[str], b: list[str], original_text: str = "") -> float:
    """Jaccard similarity between a word-set and a keyword list.

    Multi-word keywords are split so "machine learning" contributes both
    "machine" and "learning" to the word-level match.  If original_text
    contains a multi-word keyword verbatim, a +0.3 bonus applies (capped
    at 1.0).
    """
    if not a or not b:
        return 0.5  # no text or no keywords: no signal, stay neutral
    # Split multi-word keywords for word-level comparison.
    b_words: set[str] = set()
    for kw in b:
        for w in _WORD_RE.findall(kw.lower()):
            b_words.add(w)
    intersection = len(a & b_words)
    union = len(a | b_words)
    if union == 0:
        return 0.0
    score = intersection / union
    for kw in b:
        if " " in kw and kw.lower() in original_text.lower():
            score = min(1.0, score + 0.3)
            break
    return score


def _path_signal(raw_url: str) -> float:
    """Inspect URL path tokens for positive / negative signals."""
    parsed = urlparse(raw_url)
    path = parsed.path.strip("/").lower()
    if not path:
        return 0.5
    tokens = set(path.split("/"))
    if tokens & _NEGATIVE_PATH_TOKENS:
        return 0.0
    if tokens & _POSITIVE_PATH_TOKENS:
        return 1.0
    return 0.5
