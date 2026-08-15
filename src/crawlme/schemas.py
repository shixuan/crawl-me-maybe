from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class CrawlGoal(BaseModel):
    goal_id: str = Field(default_factory=_new_id)
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
    min_relevant_hits: int = 3
    relevance_threshold: float = 0.7
    depth_limit: int = 5
    domain_budget: int = 50
    extraction_spec: dict[str, Any] | None = None
    created_at: datetime.datetime = Field(default_factory=_utcnow)


class URL(BaseModel):
    raw: str
    canonical: str
    url_key: str
    scheme: str = ""
    host: str = ""
    path: str = ""
    query: str = ""
    domain: str = ""
    reg_domain: str = ""


class RawLink(BaseModel):
    href: str
    anchor: str | None = None
    snippet: str | None = None
    parent_heading: str | None = None
    position: int = 0


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


class FetchResult(BaseModel):
    item_id: str
    url_key: str
    url: URL
    status_code: int = 0
    final_url: URL | None = None
    redirects: list[URL] = Field(default_factory=list)
    headers: dict[str, Any] = Field(default_factory=dict[str, Any])
    content_type: str | None = None
    raw: bytes = b""
    fetch_duration_ms: int = 0
    fetched_at: datetime.datetime = Field(default_factory=_utcnow)
    fetch_attempt: int = 1


ExtractionStatus = Literal["OK", "DEGRADED", "FAILED"]


class Page(BaseModel):
    page_id: str = Field(default_factory=_new_id)
    url_key: str
    url: URL
    raw_html_path: str = ""
    title: str | None = None
    markdown: str | None = None
    plain_text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict[str, Any])
    text_hash: str = ""
    text_len: int = 0
    extracted_at: datetime.datetime = Field(default_factory=_utcnow)
    extraction_status: ExtractionStatus = "OK"


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
    # FeedbackStore needs the readable URL for the ranker's "seen so
    # far" history and the hub multiplier, and the title for the same
    # history lines.
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
    # domain, accumulated across tasks by the FeedbackStore (2.5).
    # RuleRanker's domain-prior factor (F4) consumes this.
    domain_priors: dict[str, float] = Field(default_factory=dict)
    pages_seen: int = 0
    fetched: int = 0


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
