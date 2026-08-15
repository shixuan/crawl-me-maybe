"""Tests for the FeedbackStore and its cross-task domain prior store."""

from __future__ import annotations

import pytest

from crawlme.schemas import AnalyzerFeedback, RankHistorySummary
from crawlme.state.feedback import (
    _DOMAIN_BOOST,
    _DOMAIN_PENALTY,
    _HUB_MULTIPLIER,
    DomainPriorStore,
    FeedbackStore,
)


def _fb(
    *,
    classification: str = "RELEVANT",
    relevance: float = 0.8,
    hub: float = 0.0,
    domain: str = "example.com",
    url: str = "https://example.com/page",
    title: str = "A page",
    topics: tuple[str, ...] = (),
    endorsed: tuple[str, ...] = (),
) -> AnalyzerFeedback:
    return AnalyzerFeedback(
        classification=classification,
        relevance_score=relevance,
        hub_score=hub,
        domain=domain,
        url=url,
        title=title,
        topics=list(topics),
        endorsed_links=list(endorsed),
    )


# -- summary -----------------------------------------------------------


def test_summary_caps_relevant_pages_and_keeps_most_recent():
    store = FeedbackStore()
    for i in range(12):
        store.update(_fb(url=f"https://example.com/{i}", title=f"Page {i}"))
    s = store.summary()

    assert isinstance(s, RankHistorySummary)
    assert s.pages_seen == 12
    assert len(s.relevant_pages) == 10
    newest = s.relevant_pages[-1]
    assert newest == {"url": "https://example.com/11", "title": "Page 11", "relevance": 0.8}


def test_non_relevant_pages_stay_out_of_summary():
    store = FeedbackStore()
    store.update(_fb(classification="RELEVANT"))
    store.update(_fb(classification="HUB"))
    store.update(_fb(classification="IRRELEVANT"))
    store.update(_fb(classification="NAVIGATION"))
    assert len(store.summary().relevant_pages) == 1


def test_hub_domains_are_top5_by_score():
    store = FeedbackStore()
    for i in range(7):
        store.update(_fb(domain=f"d{i}.com", hub=0.5 + i * 0.01))
    assert store.summary().hub_domains == ["d6.com", "d5.com", "d4.com", "d3.com", "d2.com"]


def test_weak_hubs_stay_out_of_summary():
    store = FeedbackStore()
    store.update(_fb(domain="weak.com", hub=0.4))
    assert store.summary().hub_domains == []


def test_hub_domain_keeps_best_score():
    store = FeedbackStore()
    store.update(_fb(domain="hub.com", hub=0.9))
    store.update(_fb(domain="hub.com", hub=0.6))
    assert store.summary().hub_domains == ["hub.com"]


def test_top_topics_by_frequency_capped_at_20():
    store = FeedbackStore()
    store.update(_fb(topics=("rust", "compiler")))
    store.update(_fb(topics=("rust",)))
    store.update(_fb(topics=("compiler",)))
    for i in range(21):
        store.update(_fb(topics=(f"t{i}",)))

    s = store.summary()
    assert len(s.top_topics) == 20
    assert s.top_topics[:2] == ["rust", "compiler"]


# -- domain priors -----------------------------------------------------


def test_domain_prior_averages_relevance_scores():
    store = FeedbackStore()
    store.update(_fb(classification="RELEVANT", relevance=0.9))
    store.update(_fb(classification="RELEVANT", relevance=0.7))
    store.update(_fb(classification="IRRELEVANT", relevance=0.1))
    assert store.summary().domain_priors["example.com"] == pytest.approx(0.5667, abs=1e-4)


def test_domain_priors_are_per_domain():
    store = FeedbackStore()
    store.update(_fb(domain="a.com", relevance=1.0))
    store.update(_fb(domain="b.com", relevance=0.0))
    assert store.summary().domain_priors == {"a.com": 1.0, "b.com": 0.0}


# -- multipliers -------------------------------------------------------


def test_domain_multiplier_boost_needs_full_uniform_window():
    store = FeedbackStore()
    assert store.domain_multiplier("example.com") == 1.0  # unknown domain

    store.update(_fb(classification="RELEVANT"))
    store.update(_fb(classification="HUB"))  # hubs count as relevant
    assert store.domain_multiplier("example.com") == 1.0  # window not full yet

    store.update(_fb(classification="RELEVANT"))
    assert store.domain_multiplier("example.com") == _DOMAIN_BOOST


def test_domain_multiplier_penalty_after_three_irrelevant():
    store = FeedbackStore()
    for cls in ("IRRELEVANT", "NAVIGATION", "UNKNOWN"):
        store.update(_fb(classification=cls))
    assert store.domain_multiplier("example.com") == _DOMAIN_PENALTY


def test_domain_multiplier_mixed_window_stays_neutral():
    store = FeedbackStore()
    store.update(_fb(classification="RELEVANT"))
    store.update(_fb(classification="RELEVANT"))
    store.update(_fb(classification="IRRELEVANT"))
    assert store.domain_multiplier("example.com") == 1.0


def test_domain_multiplier_window_rolls_forward():
    store = FeedbackStore()
    for _ in range(3):
        store.update(_fb(classification="RELEVANT"))
    assert store.domain_multiplier("example.com") == _DOMAIN_BOOST

    store.update(_fb(classification="IRRELEVANT"))  # window becomes R,R,I
    assert store.domain_multiplier("example.com") == 1.0


def test_hub_multiplier_applies_to_hub_pages_only():
    store = FeedbackStore()
    store.update(_fb(url="https://hub.com/front", hub=0.9, classification="AGGREGATOR"))
    store.update(_fb(url="https://hub.com/plain", hub=0.1))

    assert store.hub_multiplier("https://hub.com/front") == _HUB_MULTIPLIER
    assert store.hub_multiplier("https://hub.com/plain") == 1.0
    assert store.hub_multiplier("https://unseen.com/x") == 1.0


# -- endorsed links ----------------------------------------------------


def test_take_endorsed_drains_pairs_with_source_url():
    store = FeedbackStore()
    store.update(_fb(url="https://src.com/p", endorsed=("https://a.com/1", "https://a.com/2")))
    store.update(_fb(url="https://src.com/q", endorsed=("https://b.com/3",)))

    assert store.take_endorsed() == [
        ("https://a.com/1", "https://src.com/p"),
        ("https://a.com/2", "https://src.com/p"),
        ("https://b.com/3", "https://src.com/q"),
    ]
    assert store.take_endorsed() == []


def test_endorsed_without_source_url_is_dropped():
    store = FeedbackStore()
    store.update(_fb(url="", endorsed=("https://a.com/1",)))
    assert store.take_endorsed() == []


# -- cross-task persistence --------------------------------------------


@pytest.mark.asyncio
async def test_domain_prior_persists_across_store_instances(tmp_path):
    db = tmp_path / "feedback.db"
    s1 = FeedbackStore(DomainPriorStore(db))
    s1.update(_fb(domain="good.com", classification="RELEVANT", relevance=0.9))
    s1.update(_fb(domain="good.com", classification="RELEVANT", relevance=0.7))
    s1.update(_fb(domain="good.com", classification="IRRELEVANT", relevance=0.2))
    await s1.aclose()

    s2 = FeedbackStore(DomainPriorStore(db))
    await s2.load()
    assert s2.summary().domain_priors["good.com"] == pytest.approx(0.6)
    await s2.aclose()


@pytest.mark.asyncio
async def test_load_seeds_prior_and_run_updates_extend_it(tmp_path):
    db = tmp_path / "feedback.db"
    s1 = FeedbackStore(DomainPriorStore(db))
    s1.update(_fb(domain="good.com", classification="RELEVANT", relevance=1.0))
    await s1.aclose()

    s2 = FeedbackStore(DomainPriorStore(db))
    await s2.load()
    s2.update(_fb(domain="good.com", classification="IRRELEVANT", relevance=0.0))
    assert s2.summary().domain_priors["good.com"] == pytest.approx(0.5)
    await s2.aclose()


@pytest.mark.asyncio
async def test_aclose_is_idempotent(tmp_path):
    store = FeedbackStore(DomainPriorStore(tmp_path / "feedback.db"))
    store.update(_fb())
    await store.aclose()
    await store.aclose()  # second close is a no-op, never a hang


def test_store_without_prior_store_works_in_memory():
    store = FeedbackStore()
    store.update(_fb())
    assert store.summary().pages_seen == 1
    assert store.summary().domain_priors["example.com"] == 0.8
    store.update(_fb())
    assert store.summary().pages_seen == 2
