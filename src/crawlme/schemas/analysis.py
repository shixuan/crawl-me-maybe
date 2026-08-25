"""Analysis-stage models: page analyses and their steering payloads."""

from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from crawlme.schemas.core import _new_id, _utcnow

Classification = Literal["RELEVANT", "HUB", "AGGREGATOR", "IRRELEVANT", "NAVIGATION", "UNKNOWN"]


class ExtractedField(BaseModel):
    """One field the goal asked for, with the words that back it up.

    `evidence` is a verbatim span of the page, checked against the stored
    text before the field is kept.  A value whose evidence is not in the
    page is not recorded at all, so everything that survives can be
    pointed back at the page it came from.

    There is no "unknown" state: a field the page does not state is
    simply absent.  Saying nothing is what keeps a guessed deadline out
    of a list someone is going to act on.
    """

    value: str
    evidence: str


class AnalyzerFeedback(BaseModel):
    classification: str = "UNKNOWN"
    relevance_score: float = 0.0
    hub_score: float = 0.0
    endorsed_links: list[str] = Field(default_factory=list)
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
    # Only the fields the goal's extraction_spec asked for, and only
    # those whose evidence was found in the page.  Empty for a goal that
    # declared no spec, which is every link-graph crawl.
    extracted: dict[str, ExtractedField] = Field(default_factory=dict[str, "ExtractedField"])
    tags: list[str] = Field(default_factory=list)
    feedback: AnalyzerFeedback = Field(default_factory=AnalyzerFeedback)
    model: str = ""
    prompt_version: str = ""
    # Which field list produced `extracted`.  Part of what makes one
    # analysis the same as another, next to prompt_version and model.
    spec_version: str = ""
    tokens_used: int = 0
    analyzed_at: datetime.datetime = Field(default_factory=_utcnow)
