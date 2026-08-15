"""Task-wide LLM token accounting."""

from __future__ import annotations

import logging
from collections.abc import Callable

from crawlme.llm.errors import TokenBudgetError

logger = logging.getLogger(__name__)


class TokenBudget:
    """Task-wide LLM token accounting with a hard limit.

    Shared by every LLM consumer (Goal Enhancer, LLMRanker, Page
    Analyzer).  record() logs per-call and cumulative totals so usage
    is visible in the run log.  check() is the emergency brake: it
    raises before any call once the limit is reached.  The optional
    sink feeds the scheduler's counters, whose BUDGET_TOKENS stop
    condition then ends the crawl gracefully.
    """

    def __init__(self, limit: int, *, sink: Callable[[int], None] | None = None) -> None:
        self.limit = limit
        self.used = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self._sink = sink

    def bind_sink(self, sink: Callable[[int], None]) -> None:
        """Attach the scheduler counter sink after both objects exist.

        The budget is created before the scheduler (the LLM ranker
        needs it at construction time), so the sink cannot be passed
        in the constructor in that wiring.
        """
        self._sink = sink

    def check(self) -> None:
        if self.limit > 0 and self.used >= self.limit:
            raise TokenBudgetError(f"token budget exhausted: {self.used}/{self.limit}")

    def record(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.used += input_tokens + output_tokens
        self.calls += 1
        if self._sink is not None:
            self._sink(self.used)
        logger.info(
            "llm.tokens call=%d used=%d/%d (+%d in, +%d out)",
            self.calls,
            self.used,
            self.limit,
            input_tokens,
            output_tokens,
        )
