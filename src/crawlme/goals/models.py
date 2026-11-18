"""Crawl goals and their identity."""

from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from crawlme.util.dates import _utcnow
from crawlme.util.ids import _content_id


class Goal(BaseModel):
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
    # Meaning of the result's validity window; None disables temporal extraction.
    time_policy: str | None = None
    created_at: datetime.datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _derive_goal_id(self) -> Goal:
        """Derive identity from the prompt unless an explicit goal ID was supplied."""
        if not self.goal_id:
            self.goal_id = _content_id(self.prompt)
        return self
