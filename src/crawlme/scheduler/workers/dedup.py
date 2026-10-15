"""Group completed analyses while retaining originals on grouping failure."""

from __future__ import annotations

import logging
from typing import Any

from crawlme.dedup.grouper import Grouper, fingerprint
from crawlme.errors import ErrorHandler, Skipped
from crawlme.persist import Storage
from crawlme.runtime import CrawlGoal

logger = logging.getLogger(__name__)


class DedupWorker:
    def __init__(self, grouper: Grouper | None, *, storage: Storage, model: str, error_handler: ErrorHandler) -> None:
        self.error_handler = error_handler
        self.grouper = grouper
        self._storage = storage
        self._model = model
        self.report: dict[str, Any] = {"status": "disabled" if grouper is None else "pending"}

    async def run(self, goal: CrawlGoal) -> dict[str, Any]:
        grouper = self.grouper
        if grouper is None:
            return self.report
        rows = await self._storage.dedup_inputs(goal.goal_id)
        logger.info("grouping %d relevant items", len(rows))
        groups = await self.error_handler.call(
            "dedup",
            lambda: grouper.group(goal, rows),
            optional=True,
            repeatable=True,
            retry_invalid_output=False,
        )
        if isinstance(groups, Skipped):
            self.report = {"status": "failed", "error": str(groups.error)}
        else:
            await self._storage.save_groups(
                goal.goal_id,
                fingerprint(rows),
                [group.model_dump() for group in groups],
                model=self._model or "openai/gpt-4o-mini",
            )
            sources = len({row.get("url", row.get("item_id", row.get("analysis_id"))) for row in rows})
            self.report = {"status": "complete", "sources": sources, "items": len(rows), "groups": len(groups)}
            logger.info("dedup: %d items from %d pages grouped into %d results", len(rows), sources, len(groups))
        return self.report
