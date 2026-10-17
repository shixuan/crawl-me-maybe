"""Persisted task identity and lifecycle."""

import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from crawlme.util.dates import _utcnow
from crawlme.util.ids import _new_id

TaskState = Literal["CREATED", "RUNNING", "INTERRUPTED", "STOPPING", "COMPLETED", "FAILED"]


class Task(BaseModel):
    task_id: str = Field(default_factory=_new_id)
    goal_id: str = ""
    state: TaskState = "CREATED"
    counters: dict[str, Any] = Field(default_factory=dict[str, Any])
    start_at: datetime.datetime = Field(default_factory=_utcnow)
    end_at: datetime.datetime | None = None
    stopping_reason: str | None = None
