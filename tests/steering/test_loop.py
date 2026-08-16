"""Tests for SteeringLoop, the facade the engine holds."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.schemas import AnalyzerFeedback
from crawlme.steering.loop import SteeringLoop
from crawlme.steering.signals import InflightSignals
from crawlme.storage.sqlite.domain_prior import SqliteDomainPrior


class _StubAnalyzer:
    def __init__(self) -> None:
        self.sink = None
        self.closed = False

    def bind_sink(self, sink) -> None:
        self.sink = sink

    async def analyze(self, page, goal):
        return "result"

    async def drain_pending(self) -> None:
        pass

    async def aclose(self) -> None:
        self.closed = True


def test_forwarding_without_analyzer():
    """A credential-less loop still aggregates signals and summaries."""
    loop = SteeringLoop(analyzer=None, signals=InflightSignals())
    loop.update(AnalyzerFeedback(classification="RELEVANT", relevance_score=0.9, domain="d.com"))
    assert loop.summary().pages_seen == 1
    assert loop.analyze is not None  # callable; returns None without analyzer


@pytest.mark.asyncio
async def test_analyze_returns_none_without_analyzer():
    loop = SteeringLoop(analyzer=None, signals=InflightSignals())
    assert await loop.analyze(MagicMock(), MagicMock()) is None


def test_bind_sink_forwards_to_analyzer():
    def sink(result):
        pass

    analyzer = _StubAnalyzer()
    loop = SteeringLoop(analyzer=analyzer, signals=InflightSignals())
    loop.bind_sink(sink)
    assert analyzer.sink is sink


def test_bind_sink_is_noop_without_analyzer():
    loop = SteeringLoop(analyzer=None, signals=InflightSignals())
    loop.bind_sink(lambda r: None)  # must not raise


def test_update_forwards_to_signals():
    analyzer = _StubAnalyzer()
    loop = SteeringLoop(analyzer=analyzer, signals=InflightSignals())
    loop.update(AnalyzerFeedback(classification="HUB", hub_score=0.8, domain="d.com"))
    assert loop.summary().hub_domains == ["d.com"]
    assert loop.hub_multiplier("") == 1.0


@pytest.mark.asyncio
async def test_aclose_closes_analyzer_and_flushes_prior(tmp_path):
    analyzer = _StubAnalyzer()
    prior = SqliteDomainPrior(tmp_path / "feedback.db")
    loop = SteeringLoop(analyzer=analyzer, signals=InflightSignals(prior), prior_store=prior)
    loop.update(AnalyzerFeedback(classification="RELEVANT", relevance_score=1.0, domain="d.com"))
    await loop.aclose()
    assert analyzer.closed
    assert prior._closed


@pytest.mark.asyncio
async def test_aclose_with_mocks_awaits_both(monkeypatch):
    analyzer = MagicMock(aclose=AsyncMock())
    prior = MagicMock(close=AsyncMock())
    loop = SteeringLoop(analyzer=analyzer, signals=InflightSignals(prior), prior_store=prior)
    await loop.aclose()
    analyzer.aclose.assert_awaited_once()
    prior.close.assert_awaited_once()
