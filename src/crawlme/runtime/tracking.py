"""Join page results and source history without performing crawl actions."""

from __future__ import annotations

import logging
from typing import Any

from crawlme.discovery.harvester import Harvest
from crawlme.logging import where
from crawlme.platforms.base import PageProblem
from crawlme.runtime.state import RunState
from crawlme.schemas import AnalysisResult, Candidate, CrawlGoal, FrontierItem, Page, RankDecision, RankHistorySummary

logger = logging.getLogger(__name__)


class RunTracker:
    def __init__(self, state: RunState) -> None:
        self.state = state

    def feedback(self, goal: CrawlGoal, batch: list[Candidate]) -> tuple[RankHistorySummary, dict[str, dict[str, Any]]]:
        """Freeze the batch's source context without copying the entire crawl history."""
        sources = {c.source_url_key or "" for c in batch}
        return (
            RankHistorySummary(goal=goal.prompt, relevant_pages=[dict(p) for p in self.state.relevant_pages]),
            {key: dict(self.state.page_contexts[key]) for key in sources if key in self.state.page_contexts},
        )

    def ranked(self, batch: list[Candidate], decisions: list[RankDecision]) -> None:
        for decision in decisions:
            candidate = next((c for c in batch if c.candidate_id == decision.candidate_id), None)
            if candidate is not None:
                funnel = self.state.seeds[candidate.seed_url_key].funnel
                funnel.scored += 1
                funnel.wanted += not decision.dropped

    def discovered(self, page: Page, item: FrontierItem, harvest: Harvest) -> str | None:
        if harvest.problem is not None:
            self.problem(harvest.problem)
        candidates = harvest.candidates
        if harvest.listing:
            self.state.progress.listings_seen += 1
            self.state.progress.listings_empty += int(not candidates)
            self.state.stats.listings_stale += int(harvest.degraded)
            logger.info("read %d posts from %s", len(candidates), where(page.url.canonical))
        self.state.pages.of(page.url_key).listing = harvest.listing
        voted = self.vote(page.url_key)
        seed = self.state.pages.seed_of(item.url_key, item.seed_url_key or item.url_key)
        for candidate in candidates:
            candidate.seed_url_key = seed
            candidate.seed_ext = item.seed_ext
        funnel = self.state.seeds[seed].funnel
        funnel.fetched += 1
        funnel.discovered += len(candidates)
        self.state.stats.links_discovered += len(candidates)
        self.record_page_context(
            page.url_key,
            {
                "title": page.title or "",
                "link_count": len(candidates),
                "url": page.url.canonical,
                "depth": item.depth,
            },
        )
        self.state.pages.open(page.url_key, page.url.canonical, seed)
        return voted

    def analysis(self, result: AnalysisResult) -> str | None:
        """Tally an analysis and return the source whose retirement policy needs checking."""
        by_class = self.state.stats.analyses_by_class
        by_class[result.classification] = by_class.get(result.classification, 0) + 1
        fb = result.feedback
        # Feed relevant-page summaries into subsequent ranking prompts.
        if result.classification == "RELEVANT":
            rec = self.state.pages.by_url(fb.url or "")
            seed = rec.seed if rec else ""
            if seed:
                self.state.seeds[seed].funnel.relevant += 1
            self.state.relevant_pages.append(
                {
                    "url": fb.url,
                    "title": fb.title,
                    # Use analysis summaries for ranking history.
                    "summary": result.summary or "",
                    "relevance": round(result.relevance_score, 2),
                }
            )
            # The one line the run exists to produce.
            logger.info("found: %s (%s)", fb.title or "untitled", where(fb.url or ""))
        # Late retry results update context for future ranking batches.
        self.record_page_context(
            result.url_key,
            {
                "classification": result.classification,
                "relevance": result.relevance_score,
                "summary": result.summary or "",
            },
        )
        relevant = result.relevance_score >= self.state.limits.relevance_threshold
        # The result tally counts all relevant pages; retirement votes exclude listings.
        self.state.progress.relevant_found += relevant
        rec = self.state.pages.of(result.url_key)
        rec.relevant = relevant
        # Answered, whatever the answer. The gap between this and
        # fetched is a run that stopped before the analyzer got there.
        self.state.seeds[rec.seed].funnel.judged += 1
        return self.vote(result.url_key)

    def vote(self, url_key: str) -> str | None:
        """Count a non-listing page once and return its source for a retirement check."""
        rec = self.state.pages.of(url_key)
        if not rec.ready():
            return None
        rec.counted = True
        if rec.listing:
            return None
        state = self.state.seeds[rec.seed]
        state.window.append(bool(rec.relevant))
        return rec.seed

    def published(self, page: Page, seed: str) -> str | None:
        """Update a seed age streak from declared publication times.

        Undated pages neither advance nor reset the streak."""
        if self.state.limits.since is None or page.published_at is None:
            return None
        state = self.state.seeds[seed]
        if page.published_at >= self.state.limits.since:
            state.stale = 0
            return None
        state.stale += 1
        return seed

    def retire(self, seed: str, why: str) -> bool:
        """Record a new retirement unless recall is enabled; the engine drops queued work."""
        if not seed or self.state.limits.recall or self.state.seeds[seed].retired:
            return False
        self.state.seeds[seed].retired = why
        return True

    def record_page_context(self, url_key: str, fields: dict[str, Any]) -> None:
        """Merge analysis and extraction context for later ranking."""
        if not url_key:
            return
        self.state.page_contexts.setdefault(url_key, {}).update(fields)

    def problem(self, problem: PageProblem) -> None:
        """Count unavailable pages and record run-wide platform refusals."""
        stats = self.state.stats
        stats.not_content[problem.value] = stats.not_content.get(problem.value, 0) + 1
        if problem.refuses_the_run and not self.state.progress.refused_by:
            self.state.progress.refused_by = problem.value
            logger.warning("crawl.refused problem=%s pages=%d", problem.value, self.state.progress.pages_fetched)
