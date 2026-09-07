"""Task-wide LLM token accounting."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable

from crawlme.llm.errors import TokenBudgetError

logger = logging.getLogger(__name__)


class Stage:
    """Who spent the tokens.

    The label travels from the consumer that owns a client down to
    record(), because the budget is shared and a single total cannot
    say which stage to argue with. ORDER is the order the report
    prints them in, fixed so the same run twice reads the same way.
    """

    ANALYSIS = "analysis"
    RANKING = "ranking"
    GOAL = "goal"
    SEEDS = "seeds"
    ORDER = (ANALYSIS, RANKING, GOAL, SEEDS)


@dataclasses.dataclass
class Usage:
    """One stage's share of the bill."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def used(self) -> int:
        return self.input_tokens + self.output_tokens


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
        # Input tokens the provider served from its prefix cache.  They
        # count the same here and cost about a tenth as much, so a total
        # that does not separate them is not a bill.  Our prompts put
        # every fixed part first -- system, then goal, then fields --
        # precisely so this number can be large.
        self.cached_input_tokens = 0
        # Output the model spent thinking, billed and then discarded.
        self.reasoning_tokens = 0
        self.calls = 0
        # Per stage, so a total that says 500k can say which stage to
        # argue with. Keyed by Stage; an unlabelled call lands nowhere
        # and is still in the totals above.
        self.by_stage: dict[str, Usage] = {}
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

    def record(
        self,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        reasoning_tokens: int = 0,
        stage: str = "",
    ) -> None:
        if stage:
            u = self.by_stage.setdefault(stage, Usage())
            u.calls += 1
            u.input_tokens += input_tokens
            u.output_tokens += output_tokens
            u.cached_input_tokens += cached_tokens
            u.reasoning_tokens += reasoning_tokens
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cached_input_tokens += cached_tokens
        self.reasoning_tokens += reasoning_tokens
        self.used += input_tokens + output_tokens
        self.calls += 1
        if self._sink is not None:
            self._sink(self.used)
        logger.debug(
            "llm.tokens stage=%s call=%d used=%d/%d (+%d in, +%d out, %d cached, %d thinking)",
            stage or "-",
            self.calls,
            self.used,
            self.limit,
            input_tokens,
            output_tokens,
            cached_tokens,
            reasoning_tokens,
        )
