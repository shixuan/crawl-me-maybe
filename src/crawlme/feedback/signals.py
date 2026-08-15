"""InflightSignals: the run-scoped half of the feedback loop.

One instance per crawl run.  Every analyzed page contributes its
AnalyzerFeedback through update(), and the store aggregates it into
the guidance the schedulers and rankers read during the run:

  - RankHistorySummary material: the most recent relevant pages, the
    best hub domains, and the top topics, consumed through summary().
  - Priority multipliers (ranking.md 第 3 层): hub_multiplier() for
    links discovered on a page with strong hub quality, and
    domain_multiplier() for domains whose recent pages are
    consistently (ir)relevant.  Pure multipliers the scheduler applies
    at enqueue time; the frontier is never rewritten.
  - Endorsed links: URLs the analyzer would click itself, queued with
    their source page so the scheduler can canonicalize and inject
    them into the candidate buffer.

The cross-task half of the loop lives in domain_prior.py: each
update() also records the page's contribution into the bound
SqliteDomainPrior (best-effort, flushed on shutdown by the system).

update() is deliberately synchronous.  The analyzer's sink calls it
while fetch tasks are in flight, and everything here is a plain
dict/deque update with no awaits, so the single event loop never
preempts a call and no lock is needed.
"""

from __future__ import annotations

import logging
from collections import Counter, deque
from typing import Any

from crawlme.schemas import AnalyzerFeedback, RankHistorySummary
from crawlme.storage.contracts import DomainPrior

logger = logging.getLogger(__name__)

#: Run-scoped caps (todo 2.5).  The history handed to the rankers stays
#: compact no matter how long the crawl runs.
_MAX_RELEVANT = 10
_MAX_HUBS = 5
_MAX_TOPICS = 20

#: Multiplier policy (ranking.md 第 3 层).  A page is treated as a hub
#: by its hub_score, not its classification label.  A Hacker News front
#: page is an AGGREGATOR, but it is exactly the page whose outlinks
#: deserve the boost.
_HUB_MULTIPLIER = 1.5
_HUB_SCORE_THRESHOLD = 0.5
_DOMAIN_BOOST = 1.2
_DOMAIN_PENALTY = 0.6
_DOMAIN_WINDOW = 3

#: Classifications that count as relevant for the domain window.  HUB
#: pages are thin themselves but lead toward the goal, so a domain full
#: of hubs is a good domain.
_RELEVANT_CLASSES = frozenset({"RELEVANT", "HUB"})


class InflightSignals:
    """Run-scoped aggregation of analyzer feedback into ranking guidance.

    update() is the hook the scheduler calls from the analyzer sink;
    summary() feeds the rankers; the multipliers and take_endorsed()
    are consumed by the scheduler at enqueue time.  A bound
    *prior_store* receives every contribution for cross-task
    accumulation; without one the signals work
    purely in memory (tests).
    """

    def __init__(self, prior_store: DomainPrior | None = None) -> None:
        self._prior_store = prior_store
        # Most recent RELEVANT pages for the ranker's "seen so far".
        self._relevant: deque[dict[str, Any]] = deque(maxlen=_MAX_RELEVANT)
        # reg_domain -> best hub_score seen this run (threshold applied
        # on the way in, so presence means "is a hub").
        self._hub_scores: dict[str, float] = {}
        # Page URLs with strong hub quality, keyed by URL not domain:
        # the boost applies to that page's outlinks.
        self._hub_pages: set[str] = set()
        self._topic_counts: Counter[str] = Counter()
        # reg_domain -> rolling window of relevant booleans, newest at
        # the right end.
        self._windows: dict[str, deque[bool]] = {}
        # reg_domain -> (times_relevant, times_irrelevant, sum_relevance).
        # Seeded from the prior store by seed_prior(); update() extends
        # it, so summary() always reflects cross-task plus current-run
        # data.
        self._prior_stats: dict[str, tuple[int, int, float]] = {}
        # Endorsed links as (link, source_url); the scheduler resolves
        # each link against its source and injects it.
        self._endorsed: deque[tuple[str, str]] = deque()
        self._pages_seen = 0

    def seed_prior(self, rows: dict[str, dict[str, float]]) -> None:
        """Seed the in-memory prior from the cross-task store's rows.

        Called once at run start by the system facade, so the very
        first rankings of a new task already see past tasks' domain
        reputation.
        """
        for domain, row in rows.items():
            self._prior_stats[domain] = (
                int(row["times_relevant"]),
                int(row["times_irrelevant"]),
                float(row["sum_relevance"]),
            )
        logger.info("feedback.prior_loaded domains=%d", len(self._prior_stats))

    def update(self, feedback: AnalyzerFeedback) -> None:
        """Fold one analyzed page into the run state (sync, no awaits).

        Also buffers the page's contribution for the cross-task prior
        store when one is bound; it flushes on system shutdown.
        """
        domain = feedback.domain
        relevant = feedback.classification in _RELEVANT_CLASSES

        if feedback.classification == "RELEVANT":
            self._relevant.append(
                {
                    "url": feedback.url,
                    "title": feedback.title,
                    "relevance": round(feedback.relevance_score, 2),
                }
            )
        if feedback.hub_score >= _HUB_SCORE_THRESHOLD:
            self._hub_scores[domain] = max(self._hub_scores.get(domain, 0.0), feedback.hub_score)
            if feedback.url:
                self._hub_pages.add(feedback.url)
        for topic in feedback.topics:
            self._topic_counts[topic] += 1

        if domain:
            window = self._windows.setdefault(domain, deque(maxlen=_DOMAIN_WINDOW))
            window.append(relevant)
            times_rel, times_irrel, total = self._prior_stats.get(domain, (0, 0, 0.0))
            if relevant:
                times_rel += 1
            else:
                times_irrel += 1
            total += feedback.relevance_score
            self._prior_stats[domain] = (times_rel, times_irrel, total)
            if self._prior_store is not None:
                self._prior_store.record(domain, relevant=relevant, relevance_score=feedback.relevance_score)

        if feedback.endorsed_links and feedback.url:
            self._endorsed.extend((link, feedback.url) for link in feedback.endorsed_links)

        self._pages_seen += 1

    def summary(self) -> RankHistorySummary:
        """Compact history for the rankers (todo 2.5 caps: 10/5/20)."""
        hub_domains = [
            domain for domain, _ in sorted(self._hub_scores.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_HUBS]
        ]
        return RankHistorySummary(
            relevant_pages=list(self._relevant),
            hub_domains=hub_domains,
            top_topics=[topic for topic, _ in self._topic_counts.most_common(_MAX_TOPICS)],
            domain_priors=self.domain_priors(),
            pages_seen=self._pages_seen,
            # Best available here; the engine overrides with the true
            # fetch counter when it builds the history for ranking.
            fetched=self._pages_seen,
        )

    def domain_priors(self) -> dict[str, float]:
        """reg_domain -> average relevance across every analyzed page of that domain."""
        out: dict[str, float] = {}
        for domain, (rel, irrel, total) in self._prior_stats.items():
            n = rel + irrel
            if n > 0:
                out[domain] = round(total / n, 4)
        return out

    def hub_multiplier(self, source_url: str) -> float:
        """1.5 for links discovered on a page with strong hub quality, else 1.0."""
        return _HUB_MULTIPLIER if source_url in self._hub_pages else 1.0

    def domain_multiplier(self, reg_domain: str) -> float:
        """Boost or penalize a domain whose recent pages were uniform.

        The window must be full before it judges.  One or two pages are
        too thin to conclude anything, so they stay neutral.  RELEVANT
        and HUB count as relevant; every other classification does not.
        """
        window = self._windows.get(reg_domain)
        if window is None or len(window) < _DOMAIN_WINDOW:
            return 1.0
        if all(window):
            return _DOMAIN_BOOST
        if not any(window):
            return _DOMAIN_PENALTY
        return 1.0

    def take_endorsed(self) -> list[tuple[str, str]]:
        """Drain the endorsed-links queue as (link, source_url) pairs."""
        out = list(self._endorsed)
        self._endorsed.clear()
        return out
