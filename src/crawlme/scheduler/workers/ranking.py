"""Rank candidate batches and translate retained decisions into fetchable items."""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Any

from crawlme.pioneer.ranker import Ranker
from crawlme.schemas import URL, Candidate, CrawlGoal, FrontierItem, RankDecision, RankHistorySummary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RankedBatch:
    decisions: list[RankDecision]
    items: list[FrontierItem]


class RankingWorker:
    def __init__(self, ranker: Ranker | None) -> None:
        self.ranker = ranker

    async def rank(
        self,
        batch: list[Candidate],
        goal: CrawlGoal,
        history: RankHistorySummary,
        page_contexts: dict[str, dict[str, Any]],
    ) -> RankedBatch:
        if self.ranker is None:
            decisions = [
                RankDecision(
                    candidate_id=c.candidate_id,
                    url_key=c.url.url_key,
                    priority=0.5,
                    dropped=False,
                    ranker="none",
                    rationale="no ranker configured",
                    decided_at=datetime.datetime.now(datetime.timezone.utc),
                )
                for c in batch
            ]
        else:
            decisions = await self.ranker.rank_batch(goal, batch, history, page_contexts=page_contexts)
        items: list[FrontierItem] = []
        for d in decisions:
            if d.dropped:
                continue
            c = next((c for c in batch if c.candidate_id == d.candidate_id), None)
            items.append(
                FrontierItem(
                    url=c.url if c else URL(raw="", canonical="", url_key=d.url_key),
                    url_key=d.url_key,
                    priority=d.priority,
                    score_source=d.ranker,
                    rationale=d.rationale,
                    depth=c.depth if c else 0,
                    reg_domain=c.url.reg_domain if c else "",
                    seed_url_key=(c.seed_url_key or "") if c else "",
                )
            )
        logger.debug(
            "rank.batch candidates=%d kept=%d dropped=%d", len(batch), len(items), sum(d.dropped for d in decisions)
        )
        return RankedBatch(decisions, items)

    async def aclose(self) -> None:
        if self.ranker is not None:
            await self.ranker.aclose()
