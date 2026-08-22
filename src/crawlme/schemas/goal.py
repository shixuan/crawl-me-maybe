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
    # Stop once this many pages have been judged relevant.  0 means the
    # run has no target and stops only when a budget runs out.
    max_relevant: int = 0
    # Diagnostic mode: nothing is discarded, the rejects are ranked
    # last.  Carried on the goal because the run's stop conditions have
    # to know: a run that deliberately reads its own rejects ends with a
    # tail of misses, which is what DIMINISHING_RETURNS watches for.
    recall: bool = False
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


def spec_fields(spec: dict[str, Any] | None) -> dict[str, str]:
    """The fields a goal declares, as name -> what it holds.

    One reader for the whole codebase.  Everything that builds a prompt,
    logs a run, or reads a stored result asks here what the fields are,
    so a spec that grows a key never has to be understood twice.  An
    empty result means the goal asks to find pages rather than to
    collect anything out of them.
    """
    if not isinstance(spec, dict):
        return {}
    fields = spec.get("fields")
    if not isinstance(fields, dict):
        return {}
    return {str(k): str(v) for k, v in fields.items() if isinstance(k, str)}


def spec_version(spec: dict[str, Any] | None) -> str:
    """A short name for one extraction spec, or "" when there is none.

    This belongs to the analysis, not to the goal.  `goal_id` is
    sha256(prompt) so that the same prompt is the same goal, which is
    what replay idempotency and the goal embedding cache are built on;
    folding a model-inferred spec into it would make the same prompt
    become a new goal every time the model worded its fields
    differently.  What actually changed is how a page was read, which is
    the same kind of fact as the prompt version and the model, so it is
    recorded alongside them.
    """
    fields = spec_fields(spec)
    if not fields:
        return ""
    canonical = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
