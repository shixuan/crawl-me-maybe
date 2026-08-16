"""Ranker package: pluggable ranking strategies.

Public surface (import from crawlme.pioneer.ranker):

  Ranker         : protocol all strategies duck-type
  HybridRanker   : default multi-stage pipeline (rule -> embedding -> llm)
  RuleRanker     : v0.1 heuristic stage (7 factors, zero LLM cost)
  EmbeddingRanker: v0.1.1 semantic stage (cosine similarity, zero LLM cost)
  LLMRanker      : v0.2 final stage (batched LLM fine-ranking)
"""

from crawlme.pioneer.ranker.base import Ranker
from crawlme.pioneer.ranker.embedding import EmbeddingRanker
from crawlme.pioneer.ranker.hybrid import HybridRanker
from crawlme.pioneer.ranker.llm import LLMRanker
from crawlme.pioneer.ranker.rule import RuleRanker

__all__ = ["EmbeddingRanker", "HybridRanker", "LLMRanker", "Ranker", "RuleRanker"]
