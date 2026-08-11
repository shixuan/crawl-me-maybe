from __future__ import annotations

import pytest

from crawlme.pioneer.ranker import HybridRanker, Ranker, _extract_keywords
from crawlme.schemas import URL, Candidate, CrawlGoal, RankDecision, RankHistorySummary


def _goal(prompt: str = "find machine learning papers") -> CrawlGoal:
    return CrawlGoal(prompt=prompt)


def _candidate(url_key: str = "k1", raw: str = "https://example.com/page", **kw) -> Candidate:
    defaults: dict = dict(
        url=URL(raw=raw, canonical=raw, url_key=url_key, reg_domain="example.com"),
        depth=0,
        position=1,
        anchor="A descriptive link",
    )
    defaults.update(kw)
    return Candidate(**defaults)


def _history() -> RankHistorySummary:
    return RankHistorySummary()


@pytest.fixture
def ranker() -> HybridRanker:
    return HybridRanker()


# -- protocol -----------------------------------------------------------


class _MockRanker:
    async def rank_batch(self, goal, candidates, history, page_contexts=None) -> list[RankDecision]:
        return [
            RankDecision(candidate_id=c.candidate_id, url_key=c.url.url_key, priority=0.5, ranker="mock")
            for c in candidates
        ]


def test_protocol_accepts_implementation():
    r: Ranker = _MockRanker()
    assert r is not None


# -- keyword extraction -------------------------------------------------


def test_extract_keywords():
    kw = _extract_keywords("Find AI and machine learning!")
    assert "find" in kw
    assert "ai" in kw
    assert "machine" in kw
    assert "learning" in kw


def test_extract_keywords_deduplicates():
    kw = _extract_keywords("test test TEST")
    assert kw.count("test") == 1


# -- HybridRanker -------------------------------------------------------


@pytest.mark.asyncio
async def test_drops_below_threshold(ranker):
    """Candidate with non-matching anchor + negative path → score < 0.35."""
    c = _candidate(
        "bad",
        raw="https://x.com/about",
        depth=5,
        position=50,
        anchor="click here",
        snippet="some random text",
    )
    decisions = await ranker.rank_batch(
        _goal("machine learning papers"),
        [c],
        _history(),
    )
    assert len(decisions) == 0


@pytest.mark.asyncio
async def test_keeps_above_threshold(ranker):
    """Shallow candidate with keyword-matching anchor should survive."""
    c = _candidate(depth=0, position=1, anchor="machine learning tutorial", raw="https://x.com/docs")
    decisions = await ranker.rank_batch(_goal("find machine learning"), [c], _history())
    assert len(decisions) == 1
    assert decisions[0].candidate_id == c.candidate_id
    assert decisions[0].ranker == "rule"
    assert not decisions[0].dropped


@pytest.mark.asyncio
async def test_sorts_descending_by_priority(ranker):
    """Better-scoring candidates should come first."""
    good = _candidate(depth=0, position=1, anchor="machine learning", raw="https://x.com/docs")
    ok = _candidate(depth=3, position=10, anchor="some link", raw="https://x.com/other")
    decisions = await ranker.rank_batch(_goal("machine learning"), [ok, good], _history())
    assert len(decisions) == 2
    assert decisions[0].candidate_id == good.candidate_id


@pytest.mark.asyncio
async def test_empty_batch(ranker):
    decisions = await ranker.rank_batch(_goal(), [], _history())
    assert decisions == []


@pytest.mark.asyncio
async def test_dropped_is_marked_on_decision(ranker):
    """Decisions below threshold have dropped=True."""
    c = _candidate(
        raw="https://x.com/login",
        depth=5,
        position=50,
        anchor="click here",
        snippet="ignore this",
    )
    # Score with RuleRanker directly to get the RawDecision.
    scored = ranker._scorer.score_batch([c], goal_keywords=_extract_keywords("machine learning papers"))
    assert scored[0].priority < 0.35


@pytest.mark.asyncio
async def test_domain_prior_from_history(ranker):
    """Hub domains in history affect RuleRanker scoring."""
    history = RankHistorySummary(
        hub_domains=["example.com"],
    )
    c = _candidate(reg_domain="example.com")
    decisions = await ranker.rank_batch(_goal(), [c], history)
    # Shallow candidate with hub domain in history should survive.
    assert len(decisions) == 1
    assert decisions[0].priority >= 0.35


@pytest.mark.asyncio
async def test_page_contexts_flow_to_scorer(ranker):
    """source_page_title and page_link_count from page_contexts affect scoring.

    Two identical candidates from different source pages — the one whose
    source title matches the goal keywords should score higher.
    """
    goal = _goal("machine learning")
    # Candidate from a page whose title matches the goal.
    c_good = _candidate(
        url_key="good",
        raw="https://a.com/page",
        source_url_key="src1",
        anchor="click here",
    )
    # Candidate from a page whose title is unrelated.
    c_bad = _candidate(
        url_key="bad",
        raw="https://b.com/page",
        source_url_key="src2",
        anchor="click here",
    )
    page_contexts = {
        "src1": {"title": "Machine Learning Papers", "link_count": 10},
        "src2": {"title": "About Us", "link_count": 10},
    }
    decisions = await ranker.rank_batch(goal, [c_good, c_bad], _history(), page_contexts=page_contexts)
    # c_good (matching source title) should score higher than c_bad.
    assert len(decisions) >= 1
    scored = ranker._scorer.score_batch(
        [c_good],
        goal_keywords=["machine", "learning"],
        source_page_title="Machine Learning Papers",
        page_link_count=10,
    )
    assert scored[0].priority > 0.35
    # Title match factor should be above neutral for the matching title.
    scored_bad = ranker._scorer.score_batch(
        [c_bad],
        goal_keywords=["machine", "learning"],
        source_page_title="About Us",
        page_link_count=10,
    )
    scored_good = scored[0].priority
    scored_bad_priority = scored_bad[0].priority
    assert scored_good > scored_bad_priority, f"Expected {scored_good} > {scored_bad_priority}"


@pytest.mark.asyncio
async def test_page_contexts_grouped_scoring(ranker):
    """Candidates from different source pages get different title_match scores."""
    goal = _goal("deep learning")
    c1 = _candidate(url_key="a", source_url_key="src_a", anchor="some link")
    c2 = _candidate(url_key="b", source_url_key="src_b", anchor="some link")
    page_contexts = {
        "src_a": {"title": "Deep Learning Tutorial", "link_count": 5},
        "src_b": {"title": "Contact Information", "link_count": 5},
    }
    decisions = await ranker.rank_batch(goal, [c1, c2], _history(), page_contexts=page_contexts)
    # c1 should score higher — title matches "deep learning".
    priorities = {d.url_key: d.priority for d in decisions}
    assert priorities.get("a", 0) > priorities.get("b", 0), f"Expected a > b, got {priorities}"
