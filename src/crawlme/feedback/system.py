"""FeedbackSystem: the facade the scheduler talks to.

The engine never touches the analyzer, the signal aggregation, or the
prior database directly.  It holds one optional FeedbackSystem and
calls through it, so the whole feedback subsystem can be enabled,
disabled, or swapped as a unit.  The factory builds the real one;
tests and bare engines pass None.

The contract (protocol) and the wiring implementation live together,
mirroring how the Ranker/Fetcher/Storage protocols ship next to their
implementations.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from crawlme.feedback.analyzer import Analyzer
from crawlme.feedback.domain_prior import DomainPriorStore
from crawlme.feedback.signals import InflightSignals
from crawlme.schemas import (
    AnalysisResult,
    AnalyzerFeedback,
    CrawlGoal,
    Page,
    RankHistorySummary,
)

logger = logging.getLogger(__name__)


class FeedbackSystem(Protocol):
    """Contract for the optional feedback subsystem (see FeedbackLoop)."""

    def bind_sink(self, sink: Callable[[AnalysisResult], None]) -> None: ...

    async def analyze(self, page: Page, goal: CrawlGoal) -> AnalysisResult | None: ...

    def update(self, feedback: AnalyzerFeedback) -> None: ...

    def summary(self) -> RankHistorySummary: ...

    def hub_multiplier(self, source_url: str) -> float: ...

    def domain_multiplier(self, reg_domain: str) -> float: ...

    def take_endorsed(self) -> list[tuple[str, str]]: ...

    async def load(self) -> None: ...

    async def aclose(self) -> None: ...


class FeedbackLoop:
    """Wired implementation: analyzer + run signals + cross-task prior.

    Created by the factory when the feedback subsystem is enabled.  A
    missing analyzer (no credentials) still leaves the prior store
    active, so past tasks' domain reputation keeps informing ranking;
    with no prior store either, the signals still work in memory.
    """

    def __init__(
        self,
        analyzer: Analyzer | None,
        signals: InflightSignals,
        prior_store: DomainPriorStore | None = None,
    ) -> None:
        self._analyzer = analyzer
        self._signals = signals
        self._prior_store = prior_store

    def bind_sink(self, sink: Callable[[AnalysisResult], None]) -> None:
        if self._analyzer is not None:
            self._analyzer.bind_sink(sink)

    async def analyze(self, page: Page, goal: CrawlGoal) -> AnalysisResult | None:
        if self._analyzer is None:
            return None
        return await self._analyzer.analyze(page, goal)

    def update(self, feedback: AnalyzerFeedback) -> None:
        self._signals.update(feedback)

    def summary(self) -> RankHistorySummary:
        return self._signals.summary()

    def hub_multiplier(self, source_url: str) -> float:
        return self._signals.hub_multiplier(source_url)

    def domain_multiplier(self, reg_domain: str) -> float:
        return self._signals.domain_multiplier(reg_domain)

    def take_endorsed(self) -> list[tuple[str, str]]:
        return self._signals.take_endorsed()

    async def load(self) -> None:
        """Seed the run signals with cross-task domain reputation.

        Called once at run start, so the very first rankings of a new
        task already see past tasks' learning.
        """
        if self._prior_store is not None:
            self._signals.seed_prior(await self._prior_store.load_all())

    async def aclose(self) -> None:
        """Release analyzer resources and flush the prior database."""
        if self._analyzer is not None:
            await self._analyzer.aclose()
        if self._prior_store is not None:
            await self._prior_store.close()
