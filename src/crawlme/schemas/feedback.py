"""Feedback-subsystem models: page analyses and their signals."""

from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from crawlme.schemas.core import _new_id, _utcnow

Classification = Literal["RELEVANT", "HUB", "AGGREGATOR", "IRRELEVANT", "NAVIGATION", "UNKNOWN"]


class AnalyzerFeedback(BaseModel):
    classification: str = "UNKNOWN"
    relevance_score: float = 0.0
    hub_score: float = 0.0
    endorsed_links: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    domain: str = ""
    # Page identity the analyzer already holds at parse time.  The
    # signal aggregation needs the readable URL for the ranker's "seen
    # so far" history and the hub multiplier, and the title for the
    # same history lines.
    url: str = ""
    title: str = ""


class AnalysisResult(BaseModel):
    analysis_id: str = Field(default_factory=_new_id)
    page_id: str = ""
    url_key: str = ""
    goal_id: str = ""
    classification: Classification = "UNKNOWN"
    relevance_score: float = 0.0
    summary: str | None = None
    structured_data: dict[str, Any] = Field(default_factory=dict[str, Any])
    tags: list[str] = Field(default_factory=list)
    feedback: AnalyzerFeedback = Field(default_factory=AnalyzerFeedback)
    model: str = ""
    prompt_version: str = ""
    tokens_used: int = 0
    analyzed_at: datetime.datetime = Field(default_factory=_utcnow)
