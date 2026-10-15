"""Decide recovery from component facts and the affected operation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from crawlme.errors.errors import Error, Kind, Scope


class Action(str, Enum):
    RETRY = "retry"
    SKIP = "skip"
    ABORT = "abort"


@dataclass(frozen=True)
class ErrorContext:
    operation: str
    attempt: int
    optional: bool
    repeatable: bool
    max_attempts: int | None = None
    retry_invalid_output: bool = True


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    delay: float = 0.0


class ErrorPolicy:
    def __init__(self, *, max_attempts: int = 3, retry_delay: float = 1.0, max_delay: float = 30.0) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must include the initial attempt")
        if any(not math.isfinite(value) or value < 0 for value in (retry_delay, max_delay)):
            raise ValueError("retry delays must be finite and nonnegative")
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay
        self._max_delay = max_delay

    def decide(self, error: Error, context: ErrorContext) -> Decision:
        if error.kind == Kind.LIMIT_REACHED:
            return self._exhausted(error, context, "limit_reached")
        if error.kind in {Kind.RESOURCE_EXHAUSTED, Kind.UNKNOWN}:
            return Decision(Action.ABORT, error.kind.value)
        if error.scope == Scope.DEPENDENCY and error.kind in {
            Kind.UNAUTHENTICATED,
            Kind.FORBIDDEN,
            Kind.NOT_FOUND,
            Kind.INVALID_INPUT,
        }:
            return Decision(Action.ABORT, "dependency_failed")
        if error.kind == Kind.INVALID_OUTPUT and not context.retry_invalid_output:
            return self._exhausted(error, context, "invalid_output")
        if error.kind in {Kind.TIMEOUT, Kind.UNAVAILABLE, Kind.RATE_LIMITED, Kind.INVALID_OUTPUT, Kind.CONFLICT}:
            if not context.repeatable:
                return self._exhausted(error, context, "not_repeatable")
            if context.attempt >= (context.max_attempts if context.max_attempts is not None else self._max_attempts):
                return self._exhausted(error, context, "attempts_exhausted")
            delay = min(self._retry_delay * 2 ** (context.attempt - 1), self._max_delay)
            retry_after = error.details.get("retry_after")
            if (
                isinstance(retry_after, (int, float))
                and not isinstance(retry_after, bool)
                and math.isfinite(retry_after)
            ):
                delay = max(delay, retry_after)
            return Decision(Action.RETRY, "recoverable_error", delay)
        return self._exhausted(error, context, "operation_failed")

    @staticmethod
    def _exhausted(error: Error, context: ErrorContext, reason: str) -> Decision:
        dependency_unavailable = error.scope == Scope.DEPENDENCY and error.kind in {
            Kind.TIMEOUT,
            Kind.UNAVAILABLE,
            Kind.RATE_LIMITED,
            Kind.CONFLICT,
        }
        return Decision(Action.ABORT if dependency_unavailable or not context.optional else Action.SKIP, reason)
