"""Shared LLM errors, separated from the client to avoid a budget/client import cycle."""


class Error(Exception):
    """LLM call failure after retries are exhausted, or a permanent
    provider error.  Callers catch this to fall back to rule scoring."""


class TokenBudgetError(Error):
    """Raised before a call that would exceed the task token budget."""
