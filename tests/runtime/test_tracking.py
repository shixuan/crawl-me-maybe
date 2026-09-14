from __future__ import annotations

from crawlme.digest.harvest import Harvest
from crawlme.runtime.state import Limits, Progress, RunState, Stats
from crawlme.runtime.tracking import RunTracker
from crawlme.schemas import URL, AnalysisResult, AnalyzerFeedback, Candidate, CrawlGoal, FrontierItem, Page


def _tracking():
    return RunTracker(RunState(limits=Limits(), progress=Progress(), stats=Stats()))


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
    assert tracking.state.page_contexts["parent"]["title"] == "after"


def test_reset_uses_current_state():
    tracking = _tracking()
    old_progress = tracking.state.progress
    tracking.state.reset(goal=CrawlGoal(prompt="test"))
    tracking.analysis(AnalysisResult(url_key="p", relevance_score=0.9))
    assert tracking.state.progress.relevant_found == 1
    assert old_progress.relevant_found == 0


def test_late_analysis_joins_run_state():
    tracking = _tracking()
    state = tracking.state
    url = URL(raw="https://example.com/p", canonical="https://example.com/p", url_key="p")
    page = Page(url=url, url_key="p")
    item = FrontierItem(url=url, url_key="p", seed_url_key="seed")
    state.pages.open("p", url.canonical, "seed")
    assert tracking.discovered(page, item, Harvest([])) is None

    # A later sink result needs only page identity, not a surviving per-page container.
    result = AnalysisResult(
        url_key="p", classification="RELEVANT", relevance_score=0.9, feedback=AnalyzerFeedback(url=url.canonical)
    )
    assert tracking.analysis(result) == "seed"
    assert state.seeds["seed"].funnel.relevant == 1
    assert list(state.seeds["seed"].window) == [True]
    assert tracking.vote("p") is None
    assert state.progress.relevant_found == 1


def test_trackers_share_one_state():
    first = _tracking()
    second = RunTracker(first.state)
    first.record_page_context("p", {"title": "shared"})
    candidate = Candidate(
        url=URL(raw="https://example.com/p", canonical="https://example.com/p", url_key="p"), source_url_key="p"
    )
    _, contexts = second.feedback(CrawlGoal(prompt="test"), [candidate])
    assert contexts == {"p": {"title": "shared"}}
