"""The neutral cross-layer vocabulary (see core.py for the rules).

Every model the layers exchange lives here, organized by the layer
that owns it.  This __init__ re-exports everything so the historical
flat import surface keeps working unchanged.
"""

from crawlme.schemas.analysis import AnalysisResult, AnalyzerFeedback, Classification, ExtractedField
from crawlme.schemas.core import URL, RawLink, _content_id, _new_id, _utcnow
from crawlme.schemas.digest import ExtractionStatus, FetchResult, Page, Payload
from crawlme.schemas.goal import CrawlGoal, CrawlTask, TaskState, spec_fields, spec_version
from crawlme.schemas.pioneer import (
    Candidate,
    CandidateStatus,
    FrontierItem,
    FrontierItemStatus,
    FrontierSnapshot,
    RankDecision,
    RankHistorySummary,
)

__all__ = [
    "URL",
    "AnalysisResult",
    "AnalyzerFeedback",
    "Candidate",
    "CandidateStatus",
    "Classification",
    "CrawlGoal",
    "CrawlTask",
    "ExtractedField",
    "ExtractionStatus",
    "FetchResult",
    "FrontierItem",
    "FrontierItemStatus",
    "FrontierSnapshot",
    "Page",
    "Payload",
    "RankDecision",
    "RankHistorySummary",
    "RawLink",
    "TaskState",
    "_content_id",
    "_new_id",
    "_utcnow",
    "spec_fields",
    "spec_version",
]
