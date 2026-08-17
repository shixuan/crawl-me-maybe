"""Goal and task models: the user's intent and one run of it."""

from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from crawlme.schemas.core import _content_id, _new_id, _utcnow


class CrawlGoal(BaseModel):
    goal_id: str = ""
    prompt: str
    goal_statement: str = ""
    # LLM-curated keywords from the Goal Enhancer (2.0).  Empty means
    # the rule stage falls back to bare tokenization of the prompt.
    keywords: list[str] = Field(default_factory=list)
    since: datetime.datetime | None = None
    embedding: list[float] | None = None
    max_pages: int = 500
    # LLM token budget for the whole task (v0.2).  Sized so an
    # unspecified user can finish a typical crawl: a 300-page run
    # with LLM reranking spends roughly 100-150k tokens, and even
    # the full 500k costs cents on the default model.
    max_tokens: int = 500_000
    max_duration_sec: int = 3600
    relevance_threshold: float = 0.7
    depth_limit: int = 5
    domain_budget: int = 50
    extraction_spec: dict[str, Any] | None = None
    created_at: datetime.datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _derive_goal_id(self) -> CrawlGoal:
        """A goal is named by its prompt: same text, same goal id.

        Same-prompt replay idempotency and the cross-run goal
        embedding cache both rely on this determinism.  An explicitly
        passed goal_id still wins.
        """
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
    checkpoint_ref: str | None = None
