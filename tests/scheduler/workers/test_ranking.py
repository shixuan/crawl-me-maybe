"""Verify ranking processing through its owning component."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.analysis import AnalysisResult, AnalyzerFeedback
from crawlme.errors import ErrorHandler
from crawlme.pioneer import Candidate, RankDecision, RankHistorySummary
from crawlme.runtime import CrawlGoal
from crawlme.scheduler.workers.ranking import RankingWorker
from crawlme.urls import URL
from tests.scheduler.helpers import (
    _goal,
    _make_sched,
)


@pytest.mark.asyncio
async def test_keep_and_drop():
    batch = [
        Candidate(
            url=URL(
                raw=f"https://example.com/{i}",
                canonical=f"https://example.com/{i}",
                url_key=f"k{i}",
                reg_domain="example.com",
            ),
            depth=3,
            seed_url_key="seed",
        )
        for i in range(2)
    ]
    decisions = [
        RankDecision(
            candidate_id=c.candidate_id,
            url_key=c.url.url_key,
            priority=0.8,
            dropped=bool(i),
            ranker="test",
            rationale="why",
        )
        for i, c in enumerate(batch)
    ]
    worker = RankingWorker(
        MagicMock(rank_batch=AsyncMock(return_value=decisions)),
        storage=MagicMock(),
        frontier=MagicMock(),
        tracking=MagicMock(),
        error_handler=ErrorHandler(),
    )
    result = await worker.rank(batch, CrawlGoal(prompt="test"), RankHistorySummary(goal="test"), {})
    assert result.decisions == decisions
    assert len(result.items) == 1
    item = result.items[0]
    assert (item.url_key, item.depth, item.seed_url_key, item.priority) == ("k0", 3, "seed", 0.8)
    assert item.score_source == "test"


@pytest.mark.asyncio
async def test_no_ranker_keeps_order():
    batch = [
        Candidate(url=URL(raw=f"https://example.com/{i}", canonical=f"https://example.com/{i}", url_key=f"k{i}"))
        for i in range(3)
    ]
    result = await RankingWorker(
        None, storage=MagicMock(), frontier=MagicMock(), tracking=MagicMock(), error_handler=ErrorHandler()
    ).rank(batch, CrawlGoal(prompt="test"), RankHistorySummary(goal="test"), {})
    assert [i.url_key for i in result.items] == ["k0", "k1", "k2"]
    assert all(i.priority == 0.5 and i.score_source == "none" for i in result.items)


@pytest.mark.asyncio
async def test_verdict_to_rank():
    """Ranking predicts, analysis establishes, and what analysis
    established goes back into the next prediction.

    The prompt has read `relevant_pages` all along; between v0.3.0 and
    this fix nothing filled it, so the section silently never appeared.
    """
    sched = _make_sched()
    sched.pipeline._goal = _goal(max_pages=5)

    for i, cls in enumerate(("RELEVANT", "IRRELEVANT", "RELEVANT")):
        sched.pipeline._analysis.apply(
            AnalysisResult(
                page_id=f"p{i}",
                url_key=f"k{i}",
                classification=cls,
                relevance_score=0.9 if cls == "RELEVANT" else 0.1,
                feedback=AnalyzerFeedback(
                    classification=cls,
                    relevance_score=0.9 if cls == "RELEVANT" else 0.1,
                    url=f"https://example.com/{i}",
                    title=f"Post {i}",
                ),
            )
        )

    seen = [p["url"] for p in sched.pipeline.run_state.relevant_pages]
    assert seen == ["https://example.com/0", "https://example.com/2"]

    captured: dict = {}

    async def _rank(goal, batch, history, page_contexts=None):
        captured["history"] = history
        return []

    sched.pipeline._ranking.ranker = MagicMock(rank_batch=AsyncMock(side_effect=_rank))
    sched.pipeline._frontier.push_batch = AsyncMock()
    url = URL(raw="https://x.com/c", canonical="https://x.com/c", url_key="ck")
    await sched.pipeline._ranking.run([Candidate(url=url)], sched.pipeline._goal, sched.pipeline._events)

    assert [p["title"] for p in captured["history"].relevant_pages] == ["Post 0", "Post 2"]
    # The summary rides along, or the ranker sees only the head tag.
    assert all("summary" in p for p in captured["history"].relevant_pages)
