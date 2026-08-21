"""RuleRanker: weighted heuristic scoring, zero LLM cost.

v0.1's only ranker.  Computes a weighted-average score in [0, 1] per
candidate and applies a hard threshold: candidates below it are marked
dropped, survivors are returned sorted by priority descending.

Formula:  score = sum(weight_i * factor_i) / sum(weight_i)

The formula is source-independent but the factors are not, so the set is
a constructor argument.  GRAPH_FACTORS below is the link-graph set and
stays the default; a feed of posts carries its text directly and has no
anchor, no URL path, and no position within a page, so it will bring its
own set against this same machinery.  See docs/refactor.md G1.

GRAPH_FACTORS:

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

import dataclasses
import datetime
import math
import re
import unicodedata
from collections.abc import Callable
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


#: factors -----------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ScoreContext:
    """Everything a factor may read besides the candidate itself.

    Grouping it keeps factor signatures uniform, which is what lets the
    factor set be swapped wholesale for a different kind of source.
    """

    goal_keywords: list[str] = dataclasses.field(default_factory=list)
    source_page_title: str = ""
    page_link_count: int = 0
    domain_prior: dict[str, float] = dataclasses.field(default_factory=dict)
    # Read by time-sensitive factors.  Held here rather than called for
    # inside one, so a batch scores against a single instant and two
    # candidates never disagree about when "now" was.
    now: datetime.datetime = dataclasses.field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))


@dataclasses.dataclass(frozen=True)
class Factor:
    """One weighted signal.

    The mechanism (weighted average, normalization, breakdown for the
    rationale) is source-independent; only which factors are in the set
    is not.  A feed of posts carries its text directly and has no anchor,
    no path, and no position within a page, so it wants a different set
    against the same machinery.  See docs/refactor.md G1.
    """

    name: str
    weight: float
    score: Callable[[Candidate, ScoreContext], float]


def _anchor_match(c: Candidate, ctx: ScoreContext) -> float:
    if not ctx.goal_keywords:
        return 0.5
    return _jaccard(_words(c.anchor or ""), ctx.goal_keywords, c.anchor or "")


def _snippet_match(c: Candidate, ctx: ScoreContext) -> float:
    if not ctx.goal_keywords:
        return 0.5
    return _jaccard(_words(c.snippet or ""), ctx.goal_keywords, c.snippet or "")


def _title_match(c: Candidate, ctx: ScoreContext) -> float:
    if not ctx.goal_keywords:
        return 0.5
    return _jaccard(_words(ctx.source_page_title), ctx.goal_keywords, ctx.source_page_title)


def _domain_prior_factor(c: Candidate, ctx: ScoreContext) -> float:
    return ctx.domain_prior.get(c.url.reg_domain, 0.5)


def _depth_penalty(c: Candidate, _ctx: ScoreContext) -> float:
    return 1.0 / math.sqrt(c.depth + 1)


def _path_signal_factor(c: Candidate, _ctx: ScoreContext) -> float:
    return _path_signal(c.url.raw)


def _position_signal(c: Candidate, ctx: ScoreContext) -> float:
    if ctx.page_link_count > 0:
        return 1.0 - (c.position / ctx.page_link_count)
    return 0.5


def _text_match(c: Candidate, ctx: ScoreContext) -> float:
    """What the candidate itself says, against what the goal asked for.

    A feed post carries its own words, so this is the only factor that
    looks at content rather than at a proxy for it. It is most of the
    score for that reason.
    """
    if not ctx.goal_keywords:
        return 0.5
    return _jaccard(_words(c.text), ctx.goal_keywords, _fold(c.text))


def _recency(c: Candidate, ctx: ScoreContext) -> float:
    """Newer first, because what a feed offers expires.

    A soft preference, not a cutoff: the hard window is the goal's
    `since`, enforced once in the pre-filter. An undated candidate scores
    neutral rather than last, for the same reason it is never dropped —
    absent is not old.
    """
    if c.posted_at is None:
        return 0.5
    posted = c.posted_at if c.posted_at.tzinfo else c.posted_at.replace(tzinfo=datetime.timezone.utc)
    days = (ctx.now - posted).total_seconds() / 86400.0
    if days <= 0:
        return 1.0
    return round(_RECENCY_HALF_LIFE_DAYS / (_RECENCY_HALF_LIFE_DAYS + days), 4)


#: How fast a post's score halves with age.  A week matches how long a
#: promotion tends to stay worth reading about; it is a starting point to
#: be revised against an unbiased sample, not a measured constant.
_RECENCY_HALF_LIFE_DAYS = 7.0


#: The feed factor set.  Deliberately two.
#:
#: `domain_prior` is absent because every post on a platform shares one
#: registrable domain, so it would contribute the same constant to every
#: candidate. The signal that would help is per-account, and that needs
#: state no run keeps yet.
#:
#: `tagged_only` is absent because there is no evidence for a weight. The
#: one run measured had 21 fetched candidates and every one was the
#: account's own post, so the factor would be a guess dressed as a
#: number. The recall run is what produces an unbiased sample; weights
#: are worth revisiting then and not before.
FEED_FACTORS: tuple[Factor, ...] = (
    Factor("text_match", 0.75, _text_match),
    Factor("recency", 0.25, _recency),
)


#: The graph-traversal factor set.  Order is preserved in the rationale
#: breakdown, so appending is safe but reordering changes stored output.
GRAPH_FACTORS: tuple[Factor, ...] = (
    Factor("anchor_match", 0.30, _anchor_match),
    Factor("snippet_match", 0.15, _snippet_match),
    Factor("title_match", 0.15, _title_match),
    Factor("domain_prior", 0.15, _domain_prior_factor),
    Factor("depth", 0.10, _depth_penalty),
    Factor("path_signal", 0.10, _path_signal_factor),
    Factor("position", 0.05, _position_signal),
)


def _score_one(
    c: Candidate,
    ctx: ScoreContext,
    factors: tuple[Factor, ...] = GRAPH_FACTORS,
) -> tuple[float, dict[str, float]]:
    total_weight = sum(f.weight for f in factors)
    if total_weight <= 0:
        return 0.0, {}
    # The breakdown is rounded for readability, the score is not, so a
    # rounding artifact never moves a candidate across the threshold.
    raw = {f.name: f.score(c, ctx) for f in factors}
    numerator = sum(f.weight * raw[f.name] for f in factors)
    return numerator / total_weight, {name: round(v, 4) for name, v in raw.items()}


class RuleRanker:
    """Heuristic scoring with a hard threshold.

    v0.1: threshold 0.35 (conservative: RuleRanker is the final decision
    maker).  v0.2: 0.25 (relaxed: later stages can correct its mistakes).
    """

    def __init__(self, threshold: float = 0.35, factors: tuple[Factor, ...] = GRAPH_FACTORS) -> None:
        self._threshold = threshold
        self._factors = factors

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

    async def aclose(self) -> None:
        """Pure heuristics hold no resources."""
        return None

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
        ctx = ScoreContext(
            goal_keywords=goal_keywords or [],
            source_page_title=source_page_title,
            page_link_count=page_link_count,
            domain_prior=domain_prior or {},
        )
        decisions: list[RankDecision] = []

        for c in candidates:
            priority, factors = _score_one(c, ctx, self._factors)
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


def _format_rationale(score: float, factors: dict[str, float]) -> str:
    parts = [f"rule_score={score:.4f}"]
    parts.extend(f"{k}={v:.3f}" for k, v in factors.items())
    return " ".join(parts)


def _extract_keywords(prompt: str) -> list[str]:
    return list(dict.fromkeys(w.lower() for w in _WORD_RE.findall(prompt)))


def _build_domain_prior(history: RankHistorySummary) -> dict[str, float]:
    """Merge the feedback subsystem's per-domain averages with the hub boost.

    v0.2: history.domain_priors carries real cross-task avg_relevance
    from the feedback subsystem.  Hub domains overlay a floor of 0.75 as in
    v0.1; unseen domains stay at the neutral 0.5 default in the scorer.
    """
    prior = dict(history.domain_priors)
    for domain in history.hub_domains:
        if domain:
            prior[domain] = max(prior.get(domain, 0.0), 0.75)
    return prior


#: helpers -----------------------------------------------------------


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(_fold(text))}


def _fold(text: str) -> str:
    """Fold decorative code points onto the letters they imitate.

    Social captions are written in mathematical alphanumerics and
    fullwidth forms for emphasis, so a post titled with the bold
    "Giveaway" shares no character with the keyword "giveaway" and
    matches nothing. NFKC maps both onto plain letters, which turns a
    silent zero into a hit.
    """
    return unicodedata.normalize("NFKC", text)


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
