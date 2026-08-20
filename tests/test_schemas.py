from __future__ import annotations

import pytest
from pydantic import ValidationError

from crawlme.schemas import (
    URL,
    AnalysisResult,
    AnalyzerFeedback,
    Candidate,
    CrawlGoal,
    CrawlTask,
    FetchResult,
    FrontierItem,
    FrontierSnapshot,
    Page,
    RankDecision,
    RankHistorySummary,
    RawLink,
)


def test_crawlgoal_id_is_content_derived():
    """A goal is named by its prompt: same text, same id."""
    a = CrawlGoal(prompt="find rust posts")
    b = CrawlGoal(prompt="find rust posts")
    c = CrawlGoal(prompt="find go posts")
    assert a.goal_id == b.goal_id
    assert a.goal_id != c.goal_id
    assert len(a.goal_id) == 12
    # An explicitly passed id still wins.
    d = CrawlGoal(prompt="find rust posts", goal_id="g1")
    assert d.goal_id == "g1"


def make_url() -> URL:
    return URL(
        raw="https://example.com/page?a=1",
        canonical="https://example.com/page",
        url_key="abc123",
    )


# -- defaults other modules rely on -----------------------------------------
#
# What each model does with a value it was handed is pydantic's job, not
# this project's, so only the defaults are pinned here: they are the ones
# another module reads without setting, and a careless edit to one is
# silent everywhere else.


def test_crawl_goal_defaults():
    g = CrawlGoal(prompt="find AI funding news")
    assert g.prompt == "find AI funding news"
    assert g.goal_id
    assert (g.max_pages, g.max_tokens, g.depth_limit, g.domain_budget) == (500, 500_000, 5, 50)


def test_crawl_goal_needs_a_prompt():
    with pytest.raises(ValidationError):
        CrawlGoal()  # type: ignore[call-arg]


def test_url_defaults():
    u = URL(raw="r", canonical="c", url_key="k")
    assert (u.scheme, u.host, u.reg_domain) == ("", "", "")


def test_raw_link_defaults():
    r = RawLink(href="/page")
    assert r.anchor is None
    assert r.position == 0


def test_candidate_defaults():
    c = Candidate(url=make_url())
    assert c.candidate_id
    assert c.status == "INGESTED"
    assert c.depth == 0


def test_frontier_item_defaults():
    u = make_url()
    fi = FrontierItem(url=u, url_key=u.url_key)
    assert fi.item_id
    assert (fi.status, fi.score_source, fi.priority) == ("QUEUED", "seed", 0.0)


def test_fetch_result_defaults():
    u = make_url()
    fr = FetchResult(item_id="i1", url_key=u.url_key, url=u)
    assert (fr.status_code, fr.raw, fr.fetch_attempt) == (0, b"", 1)


def test_page_defaults():
    u = make_url()
    p = Page(url_key=u.url_key, url=u)
    assert p.page_id
    assert p.extraction_status == "OK"


def test_analyzer_feedback_defaults():
    af = AnalyzerFeedback()
    assert (af.classification, af.relevance_score, af.endorsed_links) == ("UNKNOWN", 0.0, [])


def test_analysis_result_defaults():
    ar = AnalysisResult()
    assert ar.analysis_id
    assert (ar.classification, ar.tokens_used) == ("UNKNOWN", 0)


def test_rank_decision_defaults():
    rd = RankDecision(candidate_id="c1")
    assert rd.ranker == "rule"
    assert rd.dropped is False


def test_rank_history_defaults():
    rhs = RankHistorySummary()
    assert (rhs.pages_seen, rhs.hub_domains) == (0, [])


def test_crawl_task_defaults():
    ct = CrawlTask()
    assert ct.task_id
    assert ct.state == "CREATED"


def test_frontier_snapshot_defaults():
    fs = FrontierSnapshot()
    assert fs.snapshot_id
    assert fs.visited == set()


def test_frontier_snapshot_keeps_the_pre_ordering_shape():
    """`heap` predates `ordering` and still has to load.

    Checkpoints written by an older build carry their items here, and
    dropping the field would turn every one of them into an empty
    frontier that reports itself as a finished crawl.
    """
    u = make_url()
    fs = FrontierSnapshot(
        task_id="t1",
        heap=[FrontierItem(url=u, url_key=u.url_key, priority=0.5)],
        visited={"k1", "k2"},
        budgets={"example.com": 5},
    )
    assert fs.heap[0].priority == 0.5
    assert len(fs.visited) == 2
    assert fs.budgets["example.com"] == 5


def test_candidate_carries_its_own_text_and_signals():
    """A feed post brings a caption; a link brings neither."""
    from crawlme.schemas import URL, Candidate

    url = URL(raw="https://x.com/p", canonical="https://x.com/p", url_key="k1")
    link = Candidate(url=url, anchor="click here")
    assert link.text == ""
    assert link.signals == {}

    post = Candidate(url=url, text="free tea today", signals={"account": "molly", "hashtags": ["tea"]})
    assert post.text == "free tea today"
    assert post.signals["account"] == "molly"
