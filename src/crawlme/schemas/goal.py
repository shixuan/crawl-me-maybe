"""Goal and task models: the user's intent and one run of it."""

from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from crawlme.schemas.core import _content_id, _new_id, _utcnow


class CrawlGoal(BaseModel):
    goal_id: str = ""
    prompt: str
    goal_statement: str = ""
    # Optional keywords inferred by goal enhancement.
    keywords: list[str] = Field(default_factory=list)
    since: datetime.datetime | None = None
    max_pages: int = 500
    # Shared token limit across all LLM stages.
    max_tokens: int = 500_000
    max_duration_sec: int = 3600
    relevance_threshold: float = 0.7
    # Stop once this many pages have been judged relevant.  0 means the
    # run has no target and stops only when a budget runs out.
    max_relevant: int = 0
    # Keep LLM rejections at low priority and disable source retirement.
    recall: bool = False
    depth_limit: int = 5
    domain_budget: int = 50
    extraction_spec: dict[str, Any] | None = None
    created_at: datetime.datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _derive_goal_id(self) -> CrawlGoal:
        """Derive identity from the prompt unless an explicit goal ID was supplied."""
        if not self.goal_id:
            self.goal_id = _content_id(self.prompt)
        return self


TaskState = Literal["CREATED", "RUNNING", "PAUSED", "STOPPING", "COMPLETED", "FAILED"]


class CrawlTask(BaseModel):
    task_id: str = Field(default_factory=_new_id)
    goal_id: str = ""
    state: TaskState = "CREATED"
    counters: dict[str, Any] = Field(default_factory=dict[str, Any])
    start_at: datetime.datetime = Field(default_factory=_utcnow)
    end_at: datetime.datetime | None = None
    stopping_reason: str | None = None


def spec_fields(spec: dict[str, Any] | None) -> dict[str, str]:
    """Return declared field names and descriptions, or an empty mapping."""
    if not isinstance(spec, dict):
        return {}
    fields = spec.get("fields")
    if not isinstance(fields, dict):
        return {}
    return {str(k): str(v) for k, v in fields.items() if isinstance(k, str)}


def spec_time_field(spec: dict[str, Any] | None) -> tuple[str, str] | None:
    """Return the declared event-time field and on/until meaning, or None."""
    if not isinstance(spec, dict):
        return None
    raw = spec.get("time_field")
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    kind = raw.get("kind")
    if not isinstance(name, str) or name not in spec_fields(spec):
        return None
    return name, ("on" if kind == "on" else "until")


def spec_version(spec: dict[str, Any] | None) -> str:
    """Hash the extraction specification independently of the prompt-derived goal ID."""
    fields = spec_fields(spec)
    if not fields:
        return ""
    canonical = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
