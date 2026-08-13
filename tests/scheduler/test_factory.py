from __future__ import annotations

from pathlib import Path

from crawlme.config import Settings
from crawlme.pioneer.ranker import HybridRanker
from crawlme.pioneer.ranker.embedding import EmbeddingRanker
from crawlme.pioneer.ranker.rule import RuleRanker
from crawlme.scheduler.factory import _build_ranker


def test_build_ranker_rule_only_when_no_embedding_model(tmp_path: Path):
    """Without EMBEDDING_MODEL: v0.1 behavior, rule is the sole gate."""
    cfg = Settings(result_dir=tmp_path, embedding_model="")
    ranker = _build_ranker(cfg)
    assert isinstance(ranker, HybridRanker)
    assert ranker._embedding is None
    assert ranker._llm is None
    assert isinstance(ranker._rule, RuleRanker)
    assert ranker._rule._threshold == 0.35


def test_build_ranker_wires_embedding_stage(tmp_path: Path):
    """With EMBEDDING_MODEL: rule stops dropping, embedding becomes the gate."""
    cfg = Settings(
        result_dir=tmp_path,
        embedding_model="text-embedding-3-small",
        embedding_api_key="sk-test",
        embedding_base_url="https://api.openai.com/v1",
        embedding_keep=30,
    )
    ranker = _build_ranker(cfg)
    assert isinstance(ranker, HybridRanker)
    assert ranker._llm is None
    assert isinstance(ranker._embedding, EmbeddingRanker)
    assert ranker._embedding._keep == 30
    # Rule stage passes everything through; embedding selects.
    assert ranker._rule._threshold == 0.0
