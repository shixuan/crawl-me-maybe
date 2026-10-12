"""Compose crawl checkpoints without depending on the scheduler."""

from __future__ import annotations

import asyncio
import copy
import datetime
import json
from typing import Any

from crawlme.checkpoint.checkpointable import Checkpointable
from crawlme.config import Settings
from crawlme.persist import Storage
from crawlme.schemas import CrawlGoal, CrawlTask

VERSION = 1


class Checkpointer:
    def __init__(
        self,
        *,
        storage: Storage,
        settings: Settings,
        frontier: Checkpointable,
        runtime: Checkpointable,
        analysis: Checkpointable | None,
        budget: Checkpointable | None,
        robots: Checkpointable,
    ) -> None:
        self.storage = storage
        self.settings = settings
        self.frontier = frontier
        self.runtime = runtime
        self.analysis = analysis
        self.budget = budget
        self.robots = robots
        self._lock = asyncio.Lock()
        self._goal: CrawlGoal | None = None
        self._task: CrawlTask | None = None
        self._features: dict[str, bool] = {}

    def bind(self, goal: CrawlGoal, task: CrawlTask, *, dedup: bool, ranking: bool, analysis: bool) -> None:
        self._goal, self._task = goal, task
        self._features = {"dedup": dedup, "ranking": ranking, "analysis": analysis}

    async def save(self) -> None:
        async with self._lock:
            assert self._goal is not None and self._task is not None
            # No await until the complete state has been detached from live workers.
            frontier = self.frontier.dump()
            frontier["task_id"] = self._task.task_id
            runtime = self.runtime.dump()
            work = runtime.pop("work")
            payload = {
                "version": VERSION,
                "frontier": frontier,
                "goal": self._goal.model_dump(mode="json"),
                "task": self._task.model_dump(mode="json"),
                "settings": self.settings.model_dump(mode="json", exclude={"llm_api_key", "llm_base_url"}),
                **self._features,
                "robots": self.robots.dump(),
                "state": runtime,
                "work": work,
                "retries": self.analysis.dump()["entries"] if self.analysis is not None else [],
                "budget": self.budget.dump() if self.budget is not None else None,
            }
            payload = json.loads(json.dumps(payload))
            for entry in [*payload["work"], *payload["retries"]]:
                if "page" in entry:
                    entry["page"].pop("plain_text", None)
                    entry["page"].pop("markdown", None)
            self.storage.save_snapshot(
                {
                    "snapshot_id": f"{self._task.task_id}-latest",
                    "task_id": self._task.task_id,
                    "snapshot_json": payload,
                    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
            )
            await self.storage.flush()

    async def restore(self, payload: dict[str, Any]) -> None:
        if payload.get("version") != VERSION:
            raise ValueError("unsupported checkpoint version")
        if payload["retries"] and self.analysis is None:
            raise ValueError("checkpoint needs an analyzer; configure LLM credentials before resuming")
        payload = copy.deepcopy(payload)
        for entry in [*payload["work"], *payload["retries"]]:
            if "page" in entry:
                row = await self.storage.get_page(entry["page"]["page_id"])
                if row is None:
                    raise ValueError("checkpoint references a missing page")
                entry["page"].update(plain_text=row["plain_text"], markdown=row["markdown"])
        self.runtime.restore({**payload["state"], "work": payload["work"]})
        self.frontier.restore(payload["frontier"])
        self.robots.restore(payload["robots"])
        if self.analysis is not None:
            self.analysis.restore({"entries": payload["retries"]})
        if self.budget is not None and payload["budget"] is not None:
            self.budget.restore(payload["budget"])
