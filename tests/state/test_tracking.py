from __future__ import annotations

from crawlme.schemas import URL, AnalysisResult, Candidate, CrawlGoal
from crawlme.state.context import CrawlContext, Ledger, Limits, Progress
from crawlme.state.tracking import RunTracking


def _tracking():
    return RunTracking(CrawlContext(limits=Limits(), progress=Progress(), ledger=Ledger()))


def test_feedback_isolated():
    tracking = _tracking()
    tracking.record_page_context("parent", {"title": "before"})
    tracking.record_page_context("other", {"title": "unrelated"})
    batch = [
        Candidate(
            url=URL(raw="https://example.com/p", canonical="https://example.com/p", url_key="p"),
            source_url_key="parent",
        )
    ]
    _, contexts = tracking.feedback(CrawlGoal(prompt="test"), batch)
    tracking.record_page_context("parent", {"title": "after"})
    assert contexts == {"parent": {"title": "before"}}
    contexts["parent"]["title"] = "local"
    assert tracking.page_contexts["parent"]["title"] == "after"


def test_reset_uses_current_context():
    tracking = _tracking()
    old_progress = tracking.context.progress
    tracking.context.reset(goal=CrawlGoal(prompt="test"))
    tracking.analysis(AnalysisResult(url_key="p", relevance_score=0.9))
    assert tracking.context.progress.relevant_found == 1
    assert old_progress.relevant_found == 0
