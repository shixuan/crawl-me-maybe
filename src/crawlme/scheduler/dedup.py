"""Shared grouping orchestration for crawl completion and stored-result commands."""

from __future__ import annotations

import logging
from typing import Any

from crawlme.dedup import Grouper
from crawlme.dedup.grouper import VERSION, fingerprint
from crawlme.schemas import CrawlGoal
from crawlme.storage.base import Storage

logger = logging.getLogger(__name__)


async def group_results(storage: Storage, goal: CrawlGoal, grouper: Grouper, *, model: str) -> dict[str, Any]:
    rows = await storage.dedup_inputs(goal.goal_id)
    logger.info("grouping %d relevant results", len(rows))
    groups = await grouper.group(goal, rows)
    await storage.save_groups(
        goal.goal_id,
        fingerprint(rows),
        [g.model_dump() for g in groups],
        model=model or "openai/gpt-4o-mini",
        version=VERSION,
    )
    logger.info("dedup: %d sources grouped into %d results", len(rows), len(groups))
    return {"status": "complete", "sources": len(rows), "groups": len(groups)}
