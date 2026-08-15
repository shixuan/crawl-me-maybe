"""Tests for DomainPriorStore, the cross-task half of the loop.

Persistence flows through the FeedbackLoop facade the way the engine
uses it: update() records contributions, load() seeds a fresh run's
signals, and aclose() flushes.
"""

from __future__ import annotations

import pytest

from crawlme.feedback.domain_prior import DomainPriorStore
from crawlme.feedback.signals import InflightSignals
from crawlme.feedback.system import FeedbackLoop
from crawlme.schemas import AnalyzerFeedback


def _fb(
    *,
    classification: str = "RELEVANT",
    relevance: float = 0.8,
    domain: str = "example.com",
) -> AnalyzerFeedback:
    return AnalyzerFeedback(classification=classification, relevance_score=relevance, domain=domain)


@pytest.mark.asyncio
async def test_domain_prior_persists_across_runs(tmp_path):
    db = tmp_path / "feedback.db"
    prior1 = DomainPriorStore(db)
    run1 = FeedbackLoop(analyzer=None, signals=InflightSignals(prior1), prior_store=prior1)
    run1.update(_fb(domain="good.com", classification="RELEVANT", relevance=0.9))
    run1.update(_fb(domain="good.com", classification="RELEVANT", relevance=0.7))
    run1.update(_fb(domain="good.com", classification="IRRELEVANT", relevance=0.2))
    await run1.aclose()

    prior2 = DomainPriorStore(db)
    run2 = FeedbackLoop(analyzer=None, signals=InflightSignals(prior2), prior_store=prior2)
    await run2.load()
    assert run2.summary().domain_priors["good.com"] == pytest.approx(0.6)
    await run2.aclose()


@pytest.mark.asyncio
async def test_load_seeds_prior_and_run_updates_extend_it(tmp_path):
    db = tmp_path / "feedback.db"
    prior1 = DomainPriorStore(db)
    run1 = FeedbackLoop(analyzer=None, signals=InflightSignals(prior1), prior_store=prior1)
    run1.update(_fb(domain="good.com", classification="RELEVANT", relevance=1.0))
    await run1.aclose()

    prior2 = DomainPriorStore(db)
    run2 = FeedbackLoop(analyzer=None, signals=InflightSignals(prior2), prior_store=prior2)
    await run2.load()
    run2.update(_fb(domain="good.com", classification="IRRELEVANT", relevance=0.0))
    assert run2.summary().domain_priors["good.com"] == pytest.approx(0.5)
    await run2.aclose()


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path):
    prior = DomainPriorStore(tmp_path / "feedback.db")
    prior.record("example.com", relevant=True, relevance_score=0.8)
    await prior.close()
    await prior.close()  # second close is a no-op, never a hang


def test_record_after_close_is_dropped(tmp_path):
    prior = DomainPriorStore(tmp_path / "feedback.db")
    prior.record("example.com", relevant=True, relevance_score=0.8)
    # A closed store silently drops later records: nothing to flush.
    assert len(prior._pending) == 1
