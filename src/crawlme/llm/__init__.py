"""Shared LLM client, token accounting and response parsing."""

from crawlme.llm.budget import Stage, TokenBudget
from crawlme.llm.client import Client, LLMResponse, close_litellm_clients, litellm_loaded
from crawlme.llm.errors import DependencyError, RemoteError, TokenBudgetError
from crawlme.llm.parsing import parse_json_response

__all__ = [
    "Client",
    "DependencyError",
    "LLMResponse",
    "RemoteError",
    "Stage",
    "TokenBudget",
    "TokenBudgetError",
    "close_litellm_clients",
    "litellm_loaded",
    "parse_json_response",
]
