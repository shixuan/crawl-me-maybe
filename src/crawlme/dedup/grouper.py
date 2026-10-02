"""Group analyzed results and persist grouping snapshots."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from crawlme import prompts
from crawlme.config import Settings
from crawlme.llm import Stage, TokenBudget
from crawlme.llm.client import LLMClient
from crawlme.llm.errors import LLMError
from crawlme.llm.parsing import parse_json_response
from crawlme.schemas import CrawlGoal
from crawlme.storage.base import Storage

logger = logging.getLogger(__name__)


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    members: list[str] = Field(min_length=1)
    overview: str = Field(min_length=1, max_length=4000)


class Grouping(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    groups: list[Group]


def fingerprint(rows: list[dict[str, Any]]) -> str:
    """Invalidate stored groups if their input analyses change, including replay."""
    payload = json.dumps(sorted(rows, key=lambda r: r["analysis_id"]), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


class Grouper:
    def __init__(self, client: LLMClient, *, max_chars: int) -> None:
        self.client = client
        self.max_chars = max_chars

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> Grouper | None:
        client = LLMClient.from_settings_if_configured(
            settings, budget=budget, reasoning_effort=settings.llm_dedup_reasoning_effort, stage=Stage.DEDUP
        )
        return cls(client, max_chars=settings.llm_dedup_max_chars) if client is not None else None

    async def group(self, goal: CrawlGoal, rows: list[dict[str, Any]]) -> list[Group]:
        if len(rows) < 2:
            return [Group(members=[r["analysis_id"]], overview=r.get("summary") or "Result") for r in rows]
        prompt = prompts.dedup_input(goal, rows)
        if len(prompt) + len(prompts.DEDUP_SYSTEM) > self.max_chars:
            raise LLMError("dedup input exceeds LLM_DEDUP_MAX_CHARS; original results retained")
        response = await self.client.chat(prompt, system=prompts.DEDUP_SYSTEM, json_mode=True)
        if response.truncated:
            raise LLMError("dedup response truncated; original results retained")
        data = parse_json_response(response.content)
        if data is None:
            raise LLMError("dedup response is not a JSON object")
        groups = Grouping.model_validate(data).groups
        members = [member for group in groups for member in group.members]
        expected = {r["analysis_id"] for r in rows}
        if len(members) != len(set(members)) or not set(members) <= expected:
            raise LLMError("dedup returned duplicate or unknown analysis IDs")
        # Omission is safe abstention, never deletion of an analyzed result.
        assigned = set(members)
        return groups + [
            Group(members=[r["analysis_id"]], overview=r.get("summary") or "Result")
            for r in rows
            if r["analysis_id"] not in assigned
        ]


async def group_results(storage: Storage, goal: CrawlGoal, grouper: Grouper, *, model: str) -> dict[str, Any]:
    rows = await storage.dedup_inputs(goal.goal_id)
    logger.info("grouping %d relevant results", len(rows))
    groups = await grouper.group(goal, rows)
    await storage.save_groups(
        goal.goal_id,
        fingerprint(rows),
        [g.model_dump() for g in groups],
        model=model or "openai/gpt-4o-mini",
    )
    logger.info("dedup: %d sources grouped into %d results", len(rows), len(groups))
    return {"status": "complete", "sources": len(rows), "groups": len(groups)}
