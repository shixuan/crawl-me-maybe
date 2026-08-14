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


def make_url() -> URL:
    return URL(
        raw="https://example.com/page?a=1",
        canonical="https://example.com/page",
        url_key="abc123",
    )


class TestCrawlGoal:
    def test_minimal(self):
        g = CrawlGoal(prompt="find AI funding news")
        assert g.prompt == "find AI funding news"
        assert g.goal_id
        assert g.max_pages == 500

    def test_defaults(self):
        g = CrawlGoal(prompt="test")
        assert g.max_tokens == 500_000
        assert g.depth_limit == 5
        assert g.domain_budget == 50

    def test_prompt_required(self):
        with pytest.raises(ValidationError):
            CrawlGoal()  # type: ignore[call-arg]


class TestURL:
    def test_minimal(self):
        u = URL(raw="https://x.com", canonical="https://x.com", url_key="k1")
        assert u.raw == "https://x.com"

    def test_defaults(self):
        u = URL(raw="r", canonical="c", url_key="k")
        assert u.scheme == ""
        assert u.host == ""
        assert u.reg_domain == ""


class TestRawLink:
    def test_minimal(self):
        r = RawLink(href="/page")
        assert r.href == "/page"
        assert r.anchor is None
        assert r.position == 0

    def test_full(self):
        r = RawLink(href="/a", anchor="next", snippet="click here", parent_heading="News", position=5)
        assert r.anchor == "next"
        assert r.parent_heading == "News"
        assert r.position == 5


class TestCandidate:
    def test_minimal(self):
        c = Candidate(url=make_url())
        assert c.candidate_id
        assert c.status == "INGESTED"
        assert c.depth == 0

    def test_full(self):
        c = Candidate(
            url=make_url(),
            anchor="link",
            depth=3,
            source_url_key="src",
            status="BUFFERED",
        )
        assert c.depth == 3
        assert c.status == "BUFFERED"


class TestFrontierItem:
    def test_minimal(self):
        u = make_url()
        fi = FrontierItem(url=u, url_key=u.url_key)
        assert fi.item_id
        assert fi.status == "QUEUED"
        assert fi.score_source == "seed"
        assert fi.priority == 0.0

    def test_full(self):
        u = make_url()
        fi = FrontierItem(
            url=u,
            url_key=u.url_key,
            priority=0.9,
            score_source="rule",
            depth=2,
            seq=42,
        )
        assert fi.priority == 0.9
        assert fi.score_source == "rule"
        assert fi.seq == 42


class TestFetchResult:
    def test_minimal(self):
        u = make_url()
        fr = FetchResult(item_id="i1", url_key=u.url_key, url=u)
        assert fr.status_code == 0
        assert fr.raw == b""
        assert fr.fetch_attempt == 1

    def test_with_data(self):
        u = make_url()
        fr = FetchResult(
            item_id="i1",
            url_key=u.url_key,
            url=u,
            status_code=200,
            raw=b"<html>",
            fetch_duration_ms=150,
        )
        assert fr.status_code == 200
        assert fr.raw == b"<html>"
        assert fr.fetch_duration_ms == 150


class TestPage:
    def test_minimal(self):
        u = make_url()
        p = Page(url_key=u.url_key, url=u)
        assert p.page_id
        assert p.extraction_status == "OK"

    def test_degraded(self):
        u = make_url()
        p = Page(
            url_key=u.url_key,
            url=u,
            extraction_status="DEGRADED",
            title="Partial",
        )
        assert p.extraction_status == "DEGRADED"
        assert p.title == "Partial"


class TestAnalyzerFeedback:
    def test_defaults(self):
        af = AnalyzerFeedback()
        assert af.classification == "UNKNOWN"
        assert af.relevance_score == 0.0
        assert af.endorsed_links == []


class TestAnalysisResult:
    def test_minimal(self):
        ar = AnalysisResult()
        assert ar.analysis_id
        assert ar.classification == "UNKNOWN"
        assert ar.tokens_used == 0

    def test_relevant(self):
        ar = AnalysisResult(
            classification="RELEVANT",
            relevance_score=0.95,
            summary="Great find",
            tags=["AI", "funding"],
        )
        assert ar.classification == "RELEVANT"
        assert ar.summary == "Great find"
        assert len(ar.tags) == 2


class TestRankDecision:
    def test_minimal(self):
        rd = RankDecision(candidate_id="c1")
        assert rd.candidate_id == "c1"
        assert rd.ranker == "rule"

    def test_dropped(self):
        rd = RankDecision(candidate_id="c2", dropped=True, rationale="spam")
        assert rd.dropped is True
        assert rd.rationale == "spam"


class TestRankHistorySummary:
    def test_defaults(self):
        rhs = RankHistorySummary()
        assert rhs.pages_seen == 0
        assert rhs.hub_domains == []

    def test_with_data(self):
        rhs = RankHistorySummary(
            goal="AI news",
            relevant_pages=[{"url": "x.com", "title": "X"}],
            hub_domains=["github.com"],
            pages_seen=10,
            fetched=5,
        )
        assert len(rhs.relevant_pages) == 1
        assert rhs.hub_domains == ["github.com"]


class TestCrawlTask:
    def test_defaults(self):
        ct = CrawlTask()
        assert ct.task_id
        assert ct.state == "CREATED"

    def test_state_transition(self):
        ct = CrawlTask(state="RUNNING")
        assert ct.state == "RUNNING"


class TestFrontierSnapshot:
    def test_minimal(self):
        fs = FrontierSnapshot()
        assert fs.snapshot_id
        assert fs.visited == set()

    def test_with_data(self):
        u = make_url()
        fi = FrontierItem(url=u, url_key=u.url_key, priority=0.5)
        fs = FrontierSnapshot(
            task_id="t1",
            heap=[fi],
            visited={"k1", "k2"},
            budgets={"example.com": 5},
            counters={"fetched": 42},
        )
        assert len(fs.heap) == 1
        assert fs.heap[0].priority == 0.5
        assert len(fs.visited) == 2
        assert fs.budgets["example.com"] == 5
        assert fs.counters["fetched"] == 42
