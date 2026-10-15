"""Execute error policy at pipeline boundaries and retain the first abort cause."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from crawlme.errors.errors import Error, Kind
from crawlme.errors.policy import Action, Decision, ErrorContext, ErrorPolicy

logger = logging.getLogger(__name__)
Result = TypeVar("Result")


@dataclass(frozen=True)
class Skipped:
    error: Error
    attempts: int
    reason: str


class ErrorHandler:
    def __init__(self, policy: ErrorPolicy | None = None) -> None:
        self.policy = policy if policy is not None else ErrorPolicy()
        self._failure: BaseException | None = None
        self._failed = asyncio.Event()

    def check(self) -> None:
        if self._failure is not None:
            raise self._failure

    def abort(self, error: BaseException, operation: str) -> BaseException:
        if self._failure is None:
            self._failure = self.describe(error, operation) if isinstance(error, Exception) else error
            self._failed.set()
        return self._failure

    async def watch(self, work: Awaitable[Result]) -> Result:
        """Let the pipeline cancel its work when any operation aborts."""
        task = asyncio.ensure_future(work)
        failed = asyncio.create_task(self._failed.wait())
        try:
            await asyncio.wait((task, failed), return_when=asyncio.FIRST_COMPLETED)
            self.check()
            return await task
        finally:
            task.cancel()
            failed.cancel()
            await asyncio.gather(task, failed, return_exceptions=True)

    def decide(self, error: Exception, context: ErrorContext) -> Decision:
        self.check()
        failure = self.describe(error, context.operation)
        decision = self.policy.decide(failure, context)
        if decision.action == Action.RETRY:
            outcome = f"retrying in {decision.delay:g}s"
        elif decision.action == Action.SKIP:
            outcome = "skipping this operation"
        else:
            outcome = "stopping the run"
        logger.log(
            logging.ERROR if decision.action == Action.ABORT else logging.WARNING,
            "%s failed (attempt %d): %s; %s",
            context.operation,
            context.attempt,
            failure,
            outcome,
        )
        if decision.action == Action.ABORT:
            raise self.abort(failure, context.operation)
        return decision

    async def call(
        self,
        operation: str,
        function: Callable[[], Awaitable[Result]],
        *,
        optional: bool = False,
        repeatable: bool = False,
        max_attempts: int | None = None,
        retry_invalid_output: bool = True,
    ) -> Result | Skipped:
        attempt = 1
        while True:
            self.check()
            try:
                return await function()
            except Exception as exc:
                context = ErrorContext(operation, attempt, optional, repeatable, max_attempts, retry_invalid_output)
                decision = self.decide(exc, context)
                if decision.action == Action.SKIP:
                    return Skipped(self.describe(exc, operation), attempt, decision.reason)
                await asyncio.sleep(decision.delay)
                attempt += 1

    @staticmethod
    def describe(error: Exception, operation: str) -> Error:
        if isinstance(error, Error):
            return error
        failure = Error(
            str(error) or type(error).__name__,
            source=operation,
            code="timeout" if isinstance(error, (TimeoutError, asyncio.TimeoutError)) else "unexpected_error",
            kind=Kind.TIMEOUT if isinstance(error, (TimeoutError, asyncio.TimeoutError)) else Kind.UNKNOWN,
            details={"exception_type": type(error).__name__},
        )
        failure.__cause__ = error
        return failure
