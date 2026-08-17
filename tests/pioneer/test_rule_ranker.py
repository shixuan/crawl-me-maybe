from __future__ import annotations

import pytest

from crawlme.pioneer.ranker.rule import RuleRanker, _build_domain_prior, _jaccard, _path_signal, _words
from crawlme.schemas import URL, Candidate, RankHistorySummary


def _candidate(url_key: str = "k1", raw: str = "https://example.com/page", **kw) -> Candidate:
    defaults: dict = dict(
        url=URL(raw=raw, canonical=raw, url_key=url_key, reg_domain="example.com"),
        depth=0,
        position=1,
        anchor="Introduction to machine learning",
        snippet="This page covers machine learning basics.",
    )
    defaults.update(kw)
    return Candidate(**defaults)


@pytest.fixture
def scorer() -> RuleRanker:
    return RuleRanker()


# -- helpers -----------------------------------------------------------


def test_jaccard_identical():
    assert _jaccard({"hello", "world"}, ["hello", "world"]) == 1.0


def test_jaccard_no_overlap():
    assert _jaccard({"foo"}, ["bar"]) == 0.0


def test_jaccard_empty_both():
    assert _jaccard(set(), []) == 0.5


def test_jaccard_empty_text_with_keywords():
    """Empty text with keywords present should stay neutral (no signal)."""
    assert _jaccard(set(), ["deep", "learning"]) == 0.5


def test_jaccard_phrase_bonus():
    score = _jaccard(
        {"machine", "learning"},
        ["machine learning"],
        "machine learning",
    )
    assert score > 0.5  # bonus applied


def test_words_tokenizes():
    assert _words("Hello, World! 123") == {"hello", "world", "123"}


def test_path_signal_negative():
    assert _path_signal("https://x.com/about") == 0.0
    assert _path_signal("https://x.com/login") == 0.0


def test_path_signal_positive():
    assert _path_signal("https://x.com/docs/api") == 1.0
    assert _path_signal("https://x.com/blog/post") == 1.0


def test_path_signal_neutral():
    assert _path_signal("https://x.com/products/widget") == 0.5


# -- scoring -----------------------------------------------------------


def test_baseline_neutral(scorer):
    """Without goal keywords, keyword-match factors default to 0.5."""
    decisions = scorer.score_batch([_candidate()])
    assert 0.4 < decisions[0].priority < 0.6


def test_anchor_match_boosts_score(scorer):
    """With keyword-matching anchor, score should exceed neutral baseline."""
    neutral = scorer.score_batch([_candidate(anchor="foo bar baz", snippet="")])
    matched = scorer.score_batch(
        [_candidate(anchor="deep learning tutorial", snippet="")],
        goal_keywords=["deep", "learning"],
    )
    assert matched[0].priority > neutral[0].priority


def test_depth_penalty(scorer):
    shallow = scorer.score_batch([_candidate("k1", depth=0)])
    deep = scorer.score_batch([_candidate("k2", depth=5)])
    assert deep[0].priority < shallow[0].priority


def test_domain_prior(scorer):
    known = scorer.score_batch(
        [_candidate("k1")],
        domain_prior={"example.com": 0.9},
    )
    unknown = scorer.score_batch([_candidate("k1")])
    assert known[0].priority > unknown[0].priority


def test_position_signal(scorer):
    top = scorer.score_batch(
        [_candidate("k1", position=1)],
        page_link_count=100,
    )
    bottom = scorer.score_batch(
        [_candidate("k2", position=99)],
        page_link_count=100,
    )
    assert bottom[0].priority < top[0].priority


def test_source_page_title_match(scorer):
    """Title matching goal keywords should boost score vs neutral title."""
    neutral = scorer.score_batch(
        [_candidate(anchor="foo", snippet="")],
        goal_keywords=["machine", "learning"],
        source_page_title="Unrelated Page",
    )
    matched = scorer.score_batch(
        [_candidate(anchor="foo", snippet="")],
        goal_keywords=["machine", "learning"],
        source_page_title="Machine Learning Deep Dive",
    )
    assert matched[0].priority > neutral[0].priority


def test_batch_returns_all(scorer):
    candidates = [_candidate(f"k{i}") for i in range(10)]
    decisions = scorer.score_batch(candidates)
    assert len(decisions) == 10
    assert all(d.ranker == "rule" for d in decisions)
    assert all(not d.dropped for d in decisions)


def test_rationale_includes_factor_breakdown(scorer):
    decisions = scorer.score_batch(
        [_candidate()],
        goal_keywords=["test"],
        source_page_title="Test Page",
    )
    r = decisions[0].rationale
    assert r is not None
    assert "rule_score=" in r
    assert "anchor_match=" in r
    assert "depth=" in r


def test_build_domain_prior_merges_statistics_and_hubs():
    """F4 combines the feedback subsystem's real averages with the hub boost."""
    history = RankHistorySummary(
        domain_priors={"a.com": 0.9, "b.com": 0.2},
        hub_domains=["b.com", "hub.com"],
    )
    prior = _build_domain_prior(history)
    assert prior["a.com"] == 0.9  # statistics pass through untouched
    assert prior["b.com"] == 0.75  # hub floor overrides the low average
    assert prior["hub.com"] == 0.75  # hub-only domain keeps its boost


#: factor set is swappable (G1) ------------------------------------------


def test_custom_factor_set_replaces_the_default():
    """A different kind of source brings its own factors, same machinery."""
    from crawlme.pioneer.ranker.rule import Factor, RuleRanker, ScoreContext

    calls: list[str] = []

    def _always(value: float):
        def _f(c, ctx: ScoreContext) -> float:
            calls.append(c.candidate_id)
            return value

        return _f

    scorer = RuleRanker(
        threshold=0.0,
        factors=(Factor("only_signal", 1.0, _always(0.75)),),
    )
    d = scorer.score_batch([_candidate()])[0]
    assert d.priority == pytest.approx(0.75)
    assert "only_signal=0.750" in d.rationale
    # None of the default factor names survive.
    assert "anchor_match" not in d.rationale
    assert calls == [d.candidate_id]


def test_weights_are_normalized_for_any_factor_set():
    """Weights need not sum to 1; the score stays in [0, 1]."""
    from crawlme.pioneer.ranker.rule import Factor, RuleRanker

    scorer = RuleRanker(
        threshold=0.0,
        factors=(
            Factor("a", 3.0, lambda c, ctx: 1.0),
            Factor("b", 1.0, lambda c, ctx: 0.0),
        ),
    )
    assert scorer.score_batch([_candidate()])[0].priority == pytest.approx(0.75)


def test_empty_factor_set_scores_zero():
    from crawlme.pioneer.ranker.rule import RuleRanker

    d = RuleRanker(threshold=0.0, factors=()).score_batch([_candidate()])[0]
    assert d.priority == 0.0


def test_default_factor_set_is_the_graph_one():
    from crawlme.pioneer.ranker.rule import GRAPH_FACTORS, RuleRanker

    assert RuleRanker()._factors is GRAPH_FACTORS
    assert [f.name for f in GRAPH_FACTORS] == [
        "anchor_match",
        "snippet_match",
        "title_match",
        "domain_prior",
        "depth",
        "path_signal",
        "position",
    ]
    assert sum(f.weight for f in GRAPH_FACTORS) == pytest.approx(1.0)
