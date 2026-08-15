"""Tests for InflightSignals, the run-scoped feedback aggregation."""

from __future__ import annotations

import pytest

from crawlme.feedback.signals import (
    _DOMAIN_BOOST,
    _DOMAIN_PENALTY,
    _HUB_MULTIPLIER,
    InflightSignals,
)
from crawlme.schemas import AnalyzerFeedback, RankHistorySummary


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
    signals = InflightSignals()
    for i in range(12):
        signals.update(_fb(url=f"https://example.com/{i}", title=f"Page {i}"))
    s = signals.summary()

    assert isinstance(s, RankHistorySummary)
    assert s.pages_seen == 12
    assert len(s.relevant_pages) == 10
    newest = s.relevant_pages[-1]
    assert newest == {"url": "https://example.com/11", "title": "Page 11", "relevance": 0.8}


def test_non_relevant_pages_stay_out_of_summary():
    signals = InflightSignals()
    signals.update(_fb(classification="RELEVANT"))
    signals.update(_fb(classification="HUB"))
    signals.update(_fb(classification="IRRELEVANT"))
    signals.update(_fb(classification="NAVIGATION"))
    assert len(signals.summary().relevant_pages) == 1


def test_hub_domains_are_top5_by_score():
    signals = InflightSignals()
    for i in range(7):
        signals.update(_fb(domain=f"d{i}.com", hub=0.5 + i * 0.01))
    assert signals.summary().hub_domains == ["d6.com", "d5.com", "d4.com", "d3.com", "d2.com"]


def test_weak_hubs_stay_out_of_summary():
    signals = InflightSignals()
    signals.update(_fb(domain="weak.com", hub=0.4))
    assert signals.summary().hub_domains == []


def test_hub_domain_keeps_best_score():
    signals = InflightSignals()
    signals.update(_fb(domain="hub.com", hub=0.9))
    signals.update(_fb(domain="hub.com", hub=0.6))
    assert signals.summary().hub_domains == ["hub.com"]


def test_top_topics_by_frequency_capped_at_20():
    signals = InflightSignals()
    signals.update(_fb(topics=("rust", "compiler")))
    signals.update(_fb(topics=("rust",)))
    signals.update(_fb(topics=("compiler",)))
    for i in range(21):
        signals.update(_fb(topics=(f"t{i}",)))

    s = signals.summary()
    assert len(s.top_topics) == 20
    assert s.top_topics[:2] == ["rust", "compiler"]


# -- domain priors -----------------------------------------------------


def test_domain_prior_averages_relevance_scores():
    signals = InflightSignals()
    signals.update(_fb(classification="RELEVANT", relevance=0.9))
    signals.update(_fb(classification="RELEVANT", relevance=0.7))
    signals.update(_fb(classification="IRRELEVANT", relevance=0.1))
    assert signals.summary().domain_priors["example.com"] == pytest.approx(0.5667, abs=1e-4)


def test_domain_priors_are_per_domain():
    signals = InflightSignals()
    signals.update(_fb(domain="a.com", relevance=1.0))
    signals.update(_fb(domain="b.com", relevance=0.0))
    assert signals.summary().domain_priors == {"a.com": 1.0, "b.com": 0.0}


# -- multipliers -------------------------------------------------------


def test_domain_multiplier_boost_needs_full_uniform_window():
    signals = InflightSignals()
    assert signals.domain_multiplier("example.com") == 1.0  # unknown domain

    signals.update(_fb(classification="RELEVANT"))
    signals.update(_fb(classification="HUB"))  # hubs count as relevant
    assert signals.domain_multiplier("example.com") == 1.0  # window not full yet

    signals.update(_fb(classification="RELEVANT"))
    assert signals.domain_multiplier("example.com") == _DOMAIN_BOOST


def test_domain_multiplier_penalty_after_three_irrelevant():
    signals = InflightSignals()
    for cls in ("IRRELEVANT", "NAVIGATION", "UNKNOWN"):
        signals.update(_fb(classification=cls))
    assert signals.domain_multiplier("example.com") == _DOMAIN_PENALTY


def test_domain_multiplier_mixed_window_stays_neutral():
    signals = InflightSignals()
    signals.update(_fb(classification="RELEVANT"))
    signals.update(_fb(classification="RELEVANT"))
    signals.update(_fb(classification="IRRELEVANT"))
    assert signals.domain_multiplier("example.com") == 1.0


def test_domain_multiplier_window_rolls_forward():
    signals = InflightSignals()
    for _ in range(3):
        signals.update(_fb(classification="RELEVANT"))
    assert signals.domain_multiplier("example.com") == _DOMAIN_BOOST

    signals.update(_fb(classification="IRRELEVANT"))  # window becomes R,R,I
    assert signals.domain_multiplier("example.com") == 1.0


def test_hub_multiplier_applies_to_hub_pages_only():
    signals = InflightSignals()
    signals.update(_fb(url="https://hub.com/front", hub=0.9, classification="AGGREGATOR"))
    signals.update(_fb(url="https://hub.com/plain", hub=0.1))

    assert signals.hub_multiplier("https://hub.com/front") == _HUB_MULTIPLIER
    assert signals.hub_multiplier("https://hub.com/plain") == 1.0
    assert signals.hub_multiplier("https://unseen.com/x") == 1.0


# -- endorsed links ----------------------------------------------------


def test_take_endorsed_drains_pairs_with_source_url():
    signals = InflightSignals()
    signals.update(_fb(url="https://src.com/p", endorsed=("https://a.com/1", "https://a.com/2")))
    signals.update(_fb(url="https://src.com/q", endorsed=("https://b.com/3",)))

    assert signals.take_endorsed() == [
        ("https://a.com/1", "https://src.com/p"),
        ("https://a.com/2", "https://src.com/p"),
        ("https://b.com/3", "https://src.com/q"),
    ]
    assert signals.take_endorsed() == []


def test_endorsed_without_source_url_is_dropped():
    signals = InflightSignals()
    signals.update(_fb(url="", endorsed=("https://a.com/1",)))
    assert signals.take_endorsed() == []


def test_signals_without_prior_store_work_in_memory():
    signals = InflightSignals()
    signals.update(_fb())
    assert signals.summary().pages_seen == 1
    assert signals.summary().domain_priors["example.com"] == 0.8
    signals.update(_fb())
    assert signals.summary().pages_seen == 2
