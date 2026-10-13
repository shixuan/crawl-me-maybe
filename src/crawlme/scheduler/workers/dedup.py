"""Group completed analyses while retaining originals on grouping failure."""

from __future__ import annotations

import logging
from typing import Any

from crawlme.dedup.grouper import Grouper, group_results
from crawlme.persist import Storage
from crawlme.runtime import CrawlGoal

logger = logging.getLogger(__name__)


class DedupWorker:
    def __init__(self, grouper: Grouper | None, *, storage: Storage, model: str) -> None:
        self.grouper = grouper
        self._storage = storage
        self._model = model
        self.report: dict[str, Any] = {"status": "disabled" if grouper is None else "pending"}

    async def run(self, goal: CrawlGoal) -> None:
        if self.grouper is None:
            return
        try:
            self.report = await group_results(self._storage, goal, self.grouper, model=self._model)
        except Exception as exc:
            self.report = {"status": "failed"}
            logger.warning("dedup failed; original results retained: %s", exc)
