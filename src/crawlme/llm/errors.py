"""LLM call failures.

Separate from client.py on purpose: both the client (which raises
these) and the budget (whose TokenBudgetError extends LLMError) import
them, so keeping them here avoids an import cycle between the two.
"""


class LLMError(Exception):
    """LLM call failure after retries are exhausted, or a permanent
    provider error.  Callers catch this to fall back to rule scoring."""


class TokenBudgetError(LLMError):
    """Raised before a call that would exceed the task token budget."""
