from __future__ import annotations

import pytest

from crawlme.pioneer.ranker import HybridRanker, Ranker
from crawlme.pioneer.ranker.hybrid import _blend, _merge, _survive
from crawlme.pioneer.ranker.rule import RuleRanker, _extract_keywords
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


def test_protocol_accepts_rule_ranker():
    r: Ranker = RuleRanker()
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
    """Candidate with non-matching anchor + negative path → dropped."""
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
    # Contract: one decision per input candidate, including dropped ones.
    assert len(decisions) == 1
    assert decisions[0].candidate_id == c.candidate_id
    assert decisions[0].dropped


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
async def test_kept_come_before_dropped(ranker):
    """Survivors are returned first, dropped decisions at the end."""
    good = _candidate("good", depth=0, position=1, anchor="machine learning", raw="https://x.com/docs")
    bad = _candidate("bad", depth=5, position=50, anchor="click here", raw="https://x.com/login")
    decisions = await ranker.rank_batch(_goal("machine learning"), [bad, good], _history())
    assert decisions[0].candidate_id == good.candidate_id
    assert decisions[1].candidate_id == bad.candidate_id
    assert decisions[1].dropped


@pytest.mark.asyncio
async def test_empty_batch(ranker):
    decisions = await ranker.rank_batch(_goal(), [], _history())
    assert decisions == []


@pytest.mark.asyncio
async def test_domain_prior_from_history(ranker):
    """Hub domains in history affect scoring."""
    history = RankHistorySummary(
        hub_domains=["example.com"],
    )
    c = _candidate(reg_domain="example.com")
    decisions = await ranker.rank_batch(_goal(), [c], history)
    # Shallow candidate with hub domain in history should survive.
    assert len(decisions) == 1
    assert not decisions[0].dropped
    assert decisions[0].priority >= 0.35


@pytest.mark.asyncio
async def test_page_contexts_flow_to_scorer(ranker):
    """source_page_title and page_link_count from page_contexts affect scoring.

    Two identical candidates from different source pages — the one whose
    source title matches the goal keywords should score higher.
    """
    goal = _goal("machine learning")
    c_good = _candidate(
        url_key="good",
        raw="https://a.com/page",
        source_url_key="src1",
        anchor="click here",
    )
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
    priorities = {d.url_key: d.priority for d in decisions}
    assert priorities["good"] > priorities["bad"]


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


# -- multi-stage funnel -------------------------------------------------


class _RecordingRanker:
    """Stub ranker that records which candidates it saw."""

    def __init__(self, drop_keys: set[str] | None = None, tag: str = "stub") -> None:
        self.seen: list[str] = []
        self._drop_keys = drop_keys or set()
        self._tag = tag

    async def rank_batch(self, goal, candidates, history, page_contexts=None) -> list[RankDecision]:
        self.seen = [c.candidate_id for c in candidates]
        return [
            RankDecision(
                candidate_id=c.candidate_id,
                url_key=c.url.url_key,
                priority=0.7,
                dropped=c.url.url_key in self._drop_keys,
                ranker=self._tag,
            )
            for c in candidates
        ]


@pytest.mark.asyncio
async def test_embedding_only_sees_rule_survivors():
    """Stage 2 must only receive candidates stage 1 did not drop."""
    c1 = _candidate("k1", raw="https://x.com/docs", anchor="machine learning")
    c2 = _candidate("k2", raw="https://x.com/login", depth=5, position=50, anchor="click here")
    c3 = _candidate("k3", raw="https://x.com/other", anchor="some link")

    embedding = _RecordingRanker(tag="embedding")
    hybrid = HybridRanker(rule=RuleRanker(), embedding=embedding)

    decisions = await hybrid.rank_batch(_goal("machine learning"), [c1, c2, c3], _history())
    # Stage 1 drops c2 (login + bad anchor + deep); embedding sees the rest.
    assert set(embedding.seen) == {c1.candidate_id, c3.candidate_id}
    by_id = {d.candidate_id: d for d in decisions}
    assert by_id[c2.candidate_id].dropped
    assert not by_id[c1.candidate_id].dropped
    assert by_id[c1.candidate_id].ranker == "embedding"


@pytest.mark.asyncio
async def test_llm_only_sees_embedding_survivors():
    """Stage 3 must only receive candidates stage 2 did not drop."""
    c1 = _candidate("k1", raw="https://x.com/docs", anchor="machine learning")
    c2 = _candidate("k2", raw="https://x.com/other", anchor="some link")

    embedding = _RecordingRanker(drop_keys={"k2"}, tag="embedding")
    llm = _RecordingRanker(tag="llm")
    hybrid = HybridRanker(rule=RuleRanker(), embedding=embedding, llm=llm)

    decisions = await hybrid.rank_batch(_goal("machine learning"), [c1, c2], _history())
    assert llm.seen == [c1.candidate_id]
    by_id = {d.candidate_id: d for d in decisions}
    assert by_id[c1.candidate_id].ranker == "llm"
    assert by_id[c2.candidate_id].ranker == "embedding"
    assert by_id[c2.candidate_id].dropped


# -- embedding stage fallback + blend -----------------------------------


class _FailingEmbedderRanker:
    async def rank_batch(self, goal, candidates, history, page_contexts=None) -> list[RankDecision]:
        raise RuntimeError("embedding API down")


@pytest.mark.asyncio
async def test_embedding_failure_falls_back_to_rule():
    """A dead embedding stage must not block the pipeline."""
    c = _candidate(depth=0, position=1, anchor="machine learning tutorial", raw="https://x.com/docs")
    hybrid = HybridRanker(rule=RuleRanker(), embedding=_FailingEmbedderRanker())

    decisions = await hybrid.rank_batch(_goal("machine learning"), [c], _history())
    assert len(decisions) == 1
    assert decisions[0].ranker == "rule"
    assert not decisions[0].dropped


def test_blend_combines_priorities():
    prev = [
        RankDecision(
            candidate_id="k1",
            url_key="k1",
            priority=0.5,
            dropped=False,
            ranker="rule",
            rationale="rule_score=0.5000",
        )
    ]
    new = [
        RankDecision(
            candidate_id="k1",
            url_key="k1",
            priority=0.9,
            dropped=False,
            ranker="embedding",
            rationale="emb_sim=0.9000",
        )
    ]
    blended = _blend(prev, new, 0.7)
    assert blended[0].priority == pytest.approx(0.78)  # 0.7*0.9 + 0.3*0.5
    assert blended[0].ranker == "embedding"
    assert "emb_sim=" in blended[0].rationale
    assert "rule_score=0.5000" in blended[0].rationale


# -- _survive / _merge --------------------------------------------------


def test_survive_filters_dropped():
    c1 = _candidate("k1")
    c2 = _candidate("k2")
    decisions = [
        RankDecision(candidate_id=c1.candidate_id, url_key="k1", priority=0.5, dropped=False),
        RankDecision(candidate_id=c2.candidate_id, url_key="k2", priority=0.5, dropped=True),
    ]
    survivors = _survive([c1, c2], decisions)
    assert [c.candidate_id for c in survivors] == [c1.candidate_id]


def test_merge_overlays_by_candidate_id():
    prev = [
        RankDecision(candidate_id="k1", url_key="k1", priority=0.3, dropped=False, ranker="rule"),
        RankDecision(candidate_id="k2", url_key="k2", priority=0.9, dropped=False, ranker="rule"),
    ]
    new = [
        RankDecision(candidate_id="k1", url_key="k1", priority=0.8, dropped=False, ranker="llm"),
    ]
    merged = _merge(prev, new)
    by_id = {d.candidate_id: d for d in merged}
    assert by_id["k1"].ranker == "llm"  # overwritten
    assert by_id["k2"].ranker == "rule"  # untouched
