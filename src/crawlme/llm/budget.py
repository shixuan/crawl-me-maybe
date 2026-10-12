"""Task-wide LLM token accounting."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from typing import Any

from crawlme.checkpoint import Checkpointable
from crawlme.llm.errors import TokenBudgetError

logger = logging.getLogger(__name__)


class Stage:
    """LLM consumer labels used for per-stage token accounting."""

    ANALYSIS = "analysis"
    RANKING = "ranking"
    GOAL = "goal"
    SEEDS = "seeds"
    DEDUP = "dedup"
    ORDER = (ANALYSIS, RANKING, GOAL, SEEDS, DEDUP)


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


class TokenBudget(Checkpointable):
    """Shared LLM token accounting. check() rejects new calls after the limit is reached.

    Already-running calls can exceed the limit before their usage is recorded."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.input_tokens = 0
        self.output_tokens = 0
        # Track cached input separately; it remains part of total input usage.
        self.cached_input_tokens = 0
        # Output the model spent thinking, billed and then discarded.
        self.reasoning_tokens = 0
        self.calls = 0
        # Unlabelled calls contribute to totals but not per-stage counts.
        self.by_stage: dict[str, Usage] = {}
        self._changed: Callable[[], None] = lambda: None

    def bind_changes(self, changed: Callable[[], None]) -> None:
        self._changed = changed

    def dump(self) -> dict[str, Any]:
        return {
            **{
                k: getattr(self, k)
                for k in ("used", "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "calls")
            },
            "by_stage": {k: dataclasses.asdict(v) for k, v in self.by_stage.items()},
        }

    def restore(self, data: dict[str, Any]) -> None:
        for key in ("used", "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "calls"):
            setattr(self, key, data[key])
        self.by_stage = {k: Usage(**v) for k, v in data["by_stage"].items()}
        self._changed()

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
        if input_tokens + output_tokens:
            self._changed()
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
