"""Page analyses, extracted fields and ranking feedback."""

from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from crawlme.schemas.core import _new_id, _utcnow

# Declaration order determines result display order.
Classification = Literal["RELEVANT", "IRRELEVANT", "UNKNOWN"]
CLASSIFICATIONS: tuple[str, ...] = ("RELEVANT", "IRRELEVANT", "UNKNOWN")


class ExtractedField(BaseModel):
    """A requested value and its source evidence. Fields with missing evidence are omitted."""

    value: str
    evidence: str


class AnalyzerFeedback(BaseModel):
    classification: str = "UNKNOWN"
    relevance_score: float = 0.0
    domain: str = ""
    # Page identity the analyzer already holds at parse time. The
    # ranker's "seen so far" history reads both.
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
    # Declared fields whose evidence was found in the page text.
    extracted: dict[str, ExtractedField] = Field(default_factory=dict[str, "ExtractedField"])
    tags: list[str] = Field(default_factory=list)
    feedback: AnalyzerFeedback = Field(default_factory=AnalyzerFeedback)
    model: str = ""
    prompt_version: str = ""
    # Event dates derived from the declared time field; either end may be absent.
    starts_on: datetime.date | None = None
    ends_on: datetime.date | None = None
    spec_version: str = ""
    tokens_used: int = 0
    analyzed_at: datetime.datetime = Field(default_factory=_utcnow)
