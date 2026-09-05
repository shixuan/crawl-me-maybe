"""Ranker package: how a candidate's turn in the queue is decided.

Public surface (import from crawlme.pioneer.ranker):

  Ranker    : protocol a ranking strategy duck-types
  LLMRanker : batched LLM scoring, the only stage that ranks

The rule and embedding stages were removed after measurement: over
seven crawls neither ordered better than a coin flip on most tasks,
and neither ever removed a candidate, because a top-K of 60 cannot
cut a batch of 20.  See the archive/embedding-investigation branch.
"""

from crawlme.pioneer.ranker.base import Ranker
from crawlme.pioneer.ranker.llm import LLMRanker

__all__ = ["LLMRanker", "Ranker"]
