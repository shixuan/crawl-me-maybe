from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.scheduler.workers import RankingWorker
from crawlme.schemas import URL, Candidate, CrawlGoal, RankDecision, RankHistorySummary


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
    worker = RankingWorker(MagicMock(rank_batch=AsyncMock(return_value=decisions)))
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
    result = await RankingWorker(None).rank(batch, CrawlGoal(prompt="test"), RankHistorySummary(goal="test"), {})
    assert [i.url_key for i in result.items] == ["k0", "k1", "k2"]
    assert all(i.priority == 0.5 and i.score_source == "none" for i in result.items)
