"""Pioneer-layer models: candidates, frontier, and ranking decisions."""

from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from crawlme.schemas.core import URL, _new_id, _utcnow

CandidateStatus = Literal["INGESTED", "FILTERED_OUT", "BUFFERED", "DROPPED", "ENQUEUED", "FETCHED"]


class Candidate(BaseModel):
    candidate_id: str = Field(default_factory=_new_id)
    url: URL
    anchor: str | None = None
    snippet: str | None = None
    parent_heading: str | None = None
    position: int = 0
    source_page_id: str | None = None
    source_url_key: str | None = None
    depth: int = 0
    # The text this candidate carries on its own, whatever the source
    # calls it: empty for a link (its business card lives in anchor and
    # snippet), the caption for a feed post.  The ranking funnel reads
    # this and nothing else, which is what lets it judge content instead
    # of proxies once a source can supply it.
    text: str = ""
    # When the source says this was published, if it says so at all.
    # Typed rather than left in the bag because the funnel scores on it
    # and the time window filters on it, and a key read by name would
    # fail silently on a typo: the score would simply be the default and
    # nothing would say why.  Every feed has a publication time; a link
    # has none, so None is the ordinary case.
    posted_at: datetime.datetime | None = None
    # Source-specific signals the funnel's factor set and the analyzer
    # pick from: hashtags, account, and whatever the next platform
    # brings.  A bag rather than columns, so adding a source never means
    # changing this schema.
    signals: dict[str, Any] = Field(default_factory=dict[str, Any])
    status: CandidateStatus = "INGESTED"
    discovered_at: datetime.datetime = Field(default_factory=_utcnow)


FrontierItemStatus = Literal["QUEUED", "IN_FLIGHT", "COMPLETED", "FAILED", "SKIPPED", "DROPPED"]


class FrontierItem(BaseModel):
    item_id: str = Field(default_factory=_new_id)
    url: URL
    url_key: str
    priority: float = 0.0
    score_source: str = "seed"
    rationale: str | None = None
    depth: int = 0
    reg_domain: str = ""
    status: FrontierItemStatus = "QUEUED"
    attempts: int = 0
    next_available_at: datetime.datetime = Field(default_factory=_utcnow)
    enqueued_at: datetime.datetime = Field(default_factory=_utcnow)
    seq: int = 0


class RankDecision(BaseModel):
    candidate_id: str
    url_key: str = ""
    priority: float = 0.0
    dropped: bool = False
    rationale: str | None = None
    ranker: str = "rule"
    tokens_used: int = 0
    decided_at: datetime.datetime = Field(default_factory=_utcnow)


class RankHistorySummary(BaseModel):
    goal: str = ""
    relevant_pages: list[dict[str, Any]] = Field(default_factory=list)
    hub_domains: list[str] = Field(default_factory=list)
    top_topics: list[str] = Field(default_factory=list)
    # reg_domain -> average relevance across every analyzed page of that
    # domain, accumulated across tasks by the feedback subsystem (2.5).
    # RuleRanker's domain-prior factor (F4) consumes this.
    domain_priors: dict[str, float] = Field(default_factory=dict)
    pages_seen: int = 0
    fetched: int = 0


class FrontierSnapshot(BaseModel):
    snapshot_id: str = Field(default_factory=_new_id)
    task_id: str = ""
    heap: list[FrontierItem] = Field(default_factory=list)
    pending: list[FrontierItem] = Field(default_factory=list)
    visited: set[str] = Field(default_factory=set)
    budgets: dict[str, Any] = Field(default_factory=dict[str, Any])
    counters: dict[str, Any] = Field(default_factory=dict[str, Any])
    feedback_agg: dict[str, Any] = Field(default_factory=dict[str, Any])
    created_at: datetime.datetime = Field(default_factory=_utcnow)
