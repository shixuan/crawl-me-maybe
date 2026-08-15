from __future__ import annotations

from pathlib import Path

import pytest

from crawlme.config import Settings
from crawlme.pioneer.ranker import HybridRanker
from crawlme.pioneer.ranker.embedding import (
    EmbeddingRanker,
    FastEmbedEmbedder,
    OpenAICompatibleEmbedder,
)
from crawlme.pioneer.ranker.rule import RuleRanker
from crawlme.scheduler.factory import _build_ranker, create_scheduler
from crawlme.state.feedback import FeedbackStore
from crawlme.state.storage import SqliteEmbeddingCache


def test_build_ranker_rule_only_when_provider_off(tmp_path: Path):
    """embedding_provider "" (--embedding off): v0.1 behavior, rule is the sole gate."""
    cfg = Settings(result_dir=tmp_path, embedding_provider="")
    ranker = _build_ranker(cfg)
    assert isinstance(ranker, HybridRanker)
    assert ranker._embedding is None
    assert ranker._llm is None
    assert isinstance(ranker._rule, RuleRanker)
    assert ranker._rule._threshold == 0.35


def test_build_ranker_wires_api_embedding_stage(tmp_path: Path):
    """embedding_provider api: OpenAI-compatible embedder + global cache."""
    cfg = Settings(result_dir=tmp_path, embedding_provider="api", embedding_api_key="sk-test")
    ranker = _build_ranker(cfg)
    assert isinstance(ranker, HybridRanker)
    assert ranker._llm is None
    assert isinstance(ranker._embedding, EmbeddingRanker)
    assert isinstance(ranker._embedding._embedder, OpenAICompatibleEmbedder)
    # Default API model when embedding_model is unset.
    assert ranker._embedding._embedder.model_name == "api/text-embedding-3-small"
    assert isinstance(ranker._embedding._cache, SqliteEmbeddingCache)
    # Cache lives at a fixed global path, not inside the per-run dir.
    assert ranker._embedding._cache._db_path == str(tmp_path / "embedding_cache.db")
    # Rule stage passes everything through; embedding selects.
    assert ranker._rule._threshold == 0.0


def test_build_ranker_wires_local_embedding_stage(tmp_path: Path, monkeypatch):
    """Default provider local: fastembed embedder, default model e5-small, lazy load."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    cfg = Settings(result_dir=tmp_path)
    ranker = _build_ranker(cfg)
    assert isinstance(ranker._embedding, EmbeddingRanker)
    assert isinstance(ranker._embedding._embedder, FastEmbedEmbedder)
    assert ranker._embedding._embedder.model_name.startswith(
        "local/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2@fastembed"
    )
    assert isinstance(ranker._embedding._cache, SqliteEmbeddingCache)


def test_build_ranker_model_override(tmp_path: Path):
    """embedding_model overrides the per-provider default."""
    cfg = Settings(
        result_dir=tmp_path,
        embedding_provider="api",
        embedding_model="jina-embeddings-v3",
        embedding_api_key="sk-test",
    )
    ranker = _build_ranker(cfg)
    assert ranker._embedding._embedder.model_name == "api/jina-embeddings-v3"


def test_build_ranker_local_without_package_fails_fast(tmp_path: Path, monkeypatch):
    """Missing fastembed + local provider → clear error, not silent fallback."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    cfg = Settings(result_dir=tmp_path)
    with pytest.raises(RuntimeError, match="fastembed"):
        _build_ranker(cfg)


def test_build_ranker_api_without_key_fails_fast(tmp_path: Path):
    """api provider + default endpoint + no key → clear error at startup.

    A custom EMBEDDING_BASE_URL (e.g. local Ollama) is exempt: keyless
    endpoints are legitimate.
    """
    cfg = Settings(result_dir=tmp_path, embedding_provider="api", embedding_api_key="")
    with pytest.raises(RuntimeError, match="EMBEDDING_API_KEY"):
        _build_ranker(cfg)


def test_build_ranker_api_keyless_custom_endpoint_allowed(tmp_path: Path):
    """Custom EMBEDDING_BASE_URL without a key is fine (self-hosted endpoints)."""
    cfg = Settings(
        result_dir=tmp_path,
        embedding_provider="api",
        embedding_base_url="http://localhost:11434/v1",
        embedding_api_key="",
    )
    ranker = _build_ranker(cfg)
    assert ranker._embedding._embedder.model_name == "api/text-embedding-3-small"


# -- v0.2 LLM stage -----------------------------------------------------


def test_build_ranker_llm_relaxes_rule_threshold(tmp_path: Path):
    """LLM stage + embedding off: the rule threshold relaxes to 0.25 so
    the coarse filter favors recall; the LLM is the final gate."""
    cfg = Settings(result_dir=tmp_path, embedding_provider="")
    llm = object()
    ranker = _build_ranker(cfg, llm=llm)
    assert ranker._llm is llm
    assert ranker._embedding is None
    assert ranker._rule._threshold == 0.25


def test_build_ranker_llm_with_embedding_stage(tmp_path: Path, monkeypatch):
    """LLM + embedding: rule passes everything through, embedding top-K
    gates, and the LLM fine-ranks the survivors."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    cfg = Settings(result_dir=tmp_path)
    llm = object()
    ranker = _build_ranker(cfg, llm=llm)
    assert ranker._llm is llm
    assert ranker._rule._threshold == 0.0
    assert isinstance(ranker._embedding, EmbeddingRanker)


def test_build_ranker_without_llm_keeps_v01_defaults(tmp_path: Path):
    """No LLM stage: rule-only pipeline stays at the v0.1 threshold."""
    cfg = Settings(result_dir=tmp_path, embedding_provider="")
    ranker = _build_ranker(cfg, llm=None)
    assert ranker._llm is None
    assert ranker._rule._threshold == 0.35


# -- v0.2 analyzer stage ------------------------------------------------


def test_create_scheduler_wires_analyzer_with_sink(tmp_path: Path):
    """The analyzer reaches the engine, which binds its persistence sink."""
    captured: dict = {}

    class _StubAnalyzer:
        def bind_sink(self, sink):
            captured["sink"] = sink

        async def analyze(self, page, goal):
            return None

        async def aclose(self):
            pass

    analyzer = _StubAnalyzer()
    cfg = Settings(result_dir=tmp_path, embedding_provider="")
    sched = create_scheduler(cfg, analyzer=analyzer)
    assert sched._analyzer is analyzer
    assert "sink" in captured  # bound by the engine at construction


def test_create_scheduler_without_analyzer_leaves_stage_off(tmp_path: Path):
    cfg = Settings(result_dir=tmp_path, embedding_provider="")
    sched = create_scheduler(cfg)
    assert sched._analyzer is None


def test_create_scheduler_injects_context(tmp_path: Path, monkeypatch):
    """The context created by the factory reaches the engine and the
    embedding stage, so every tally accumulates in one shared object."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    cfg = Settings(result_dir=tmp_path)
    sched = create_scheduler(cfg)
    assert sched.context is sched._ctx
    assert sched._ranker._embedding._stats is sched._ctx.stats


def test_create_scheduler_wires_feedback_store(tmp_path: Path, monkeypatch):
    """The factory builds the feedback loop with its prior DB at
    result_dir/feedback.db, the global file shared across tasks."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    cfg = Settings(result_dir=tmp_path)
    sched = create_scheduler(cfg)

    assert isinstance(sched._feedback, FeedbackStore)
    assert sched._feedback._prior_store is not None
    assert Path(sched._feedback._prior_store._db_path) == tmp_path / "feedback.db"
