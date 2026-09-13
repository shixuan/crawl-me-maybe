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
    source_url_key: str | None = None
    # Original seed identity, filled by the scheduler and inherited by descendants.
    seed_url_key: str = ""
    depth: int = 0
    # Source-provided content, such as a feed caption, available before fetching.
    text: str = ""
    # Proposed by the enhancer, not named by the user. The buffer gives
    # these a smaller share of its turns.
    seed_ext: bool = False
    # Source-declared publication time, if known.
    posted_at: datetime.datetime | None = None
    # Adapter metadata such as author, engagement and ownership flags.
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
    # Persist original seed ownership so resumed descendants keep their source grouping.
    seed_url_key: str = ""
    # Carried through so a resumed run keeps the smaller share.
    seed_ext: bool = False
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
    """Previous crawl findings consumed by the ranking prompt."""

    goal: str = ""
    relevant_pages: list[dict[str, Any]] = Field(default_factory=list)


class FrontierSnapshot(BaseModel):
    snapshot_id: str = Field(default_factory=_new_id)
    task_id: str = ""
    # Opaque queue state serialized by the ordering implementation.
    ordering: dict[str, Any] = Field(default_factory=dict[str, Any])
    # Pending unranked candidates included in checkpoints.
    waiting: dict[str, Any] = Field(default_factory=dict[str, Any])
    # The shape checkpoints were written in before `ordering` existed.
    # Read on restore so an older checkpoint still resumes; no longer
    # written.
    heap: list[FrontierItem] = Field(default_factory=list)
    pending: list[FrontierItem] = Field(default_factory=list)
    visited: set[str] = Field(default_factory=set)
    budgets: dict[str, Any] = Field(default_factory=dict[str, Any])
    counters: dict[str, Any] = Field(default_factory=dict[str, Any])
    created_at: datetime.datetime = Field(default_factory=_utcnow)
