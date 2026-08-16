"""The LLM access layer: client, token budget, and JSON parsing.

Neutral infrastructure consumed by every LLM-using stage (Goal
Enhancer, LLMRanker, PageAnalyzer), kept outside the state and
pioneer layers so none of them owns it.
"""

from crawlme.llm.budget import TokenBudget
from crawlme.llm.client import LLMClient, LLMResponse, close_litellm_clients, litellm_loaded
from crawlme.llm.errors import LLMError, TokenBudgetError
from crawlme.llm.parsing import parse_json_response

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "TokenBudget",
    "TokenBudgetError",
    "close_litellm_clients",
    "litellm_loaded",
    "parse_json_response",
]
