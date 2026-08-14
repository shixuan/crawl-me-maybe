from __future__ import annotations

import json
import sys

import httpx
import pytest

from crawlme.pioneer.ranker.embedding import (
    EmbeddingRanker,
    FastEmbedEmbedder,
    OpenAICompatibleEmbedder,
    _content_hash,
    _cosine,
    _normalize,
    _text_for,
    _truncate,
)
from crawlme.schemas import URL, Candidate, CrawlGoal, RankHistorySummary


def _candidate(url_key: str = "k1", raw: str = "https://example.com/page", **kw) -> Candidate:
    defaults: dict = dict(
        url=URL(raw=raw, canonical=raw, url_key=url_key, reg_domain="example.com"),
        depth=0,
        position=1,
        anchor="machine learning tutorial",
        snippet="covers the basics",
        parent_heading="Getting started",
    )
    defaults.update(kw)
    return Candidate(**defaults)


def _goal(prompt: str = "find machine learning papers") -> CrawlGoal:
    return CrawlGoal(prompt=prompt)


def _history() -> RankHistorySummary:
    return RankHistorySummary()


class _StubEmbedder:
    """Fixed vectors keyed by text; records every batch it was asked for."""

    def __init__(self, vectors: dict[str, list[float]], goal_vector: list[float] | None = None) -> None:
        self._vectors = vectors
        self._goal_vector = goal_vector or [1.0, 0.0, 0.0]
        self.calls: list[list[str]] = []

    @property
    def model_name(self) -> str:
        return "test/stub"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vectors.get(t, self._goal_vector) for t in texts]


class _DictCache:
    """In-memory EmbeddingCache for testing cache-aside behavior."""

    def __init__(self) -> None:
        self._store: dict[str, list[float]] = {}
        self.puts: list[list[tuple[str, list[float]]]] = []

    async def get_vectors(self, content_hashes: list[str]) -> dict[str, list[float]]:
        return {h: self._store[h] for h in content_hashes if h in self._store}

    async def put_vectors(self, entries: list[tuple[str, list[float]]], model: str) -> None:
        self.puts.append(list(entries))
        for h, v in entries:
            self._store[h] = v


class _FailingEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise httpx.ConnectError("embedding API down")


# -- cosine ------------------------------------------------------------


def test_cosine_identical():
    assert _cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_orthogonal():
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_mismatched_lengths():
    assert _cosine([1.0, 0.0], [1.0]) == 0.0


def test_cosine_zero_vector():
    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


# -- text composition --------------------------------------------------


def test_text_for_joins_link_context():
    c = _candidate(anchor="deep learning", snippet="neural nets", parent_heading="Tutorials")
    assert _text_for(c, None) == "deep learning neural nets Tutorials"


def test_text_for_appends_source_title():
    c = _candidate(anchor="click here", snippet=None, parent_heading=None, source_url_key="src1")
    ctx = {"src1": {"title": "Deep Learning Papers", "link_count": 5}}
    assert _text_for(c, ctx) == "click here Deep Learning Papers"


def test_text_for_falls_back_to_url():
    c = _candidate(anchor=None, snippet=None, parent_heading=None, raw="https://x.com/only-hope")
    assert _text_for(c, None) == "https://x.com/only-hope"


def test_text_for_truncates_long_texts():
    long_anchor = "word " * 500  # 2500 chars
    c = _candidate(anchor=long_anchor, snippet=None, parent_heading=None)
    assert len(_text_for(c, None)) == 512


def test_truncate_short_text_untouched():
    assert _truncate("hello world") == "hello world"


# -- rank_batch --------------------------------------------------------


@pytest.mark.asyncio
async def test_ranks_by_similarity_and_drops_beyond_keep():
    goal = _goal()
    embedder = _StubEmbedder(
        vectors={
            "close": [1.0, 0.0, 0.0],
            "far": [0.0, 0.0, 1.0],
            "mid": [0.6, 0.0, 0.8],
        }
    )
    ranker = EmbeddingRanker(embedder, keep=2)

    c_close = _candidate("close", anchor="close", snippet=None, parent_heading=None)
    c_far = _candidate("far", anchor="far", snippet=None, parent_heading=None)
    c_mid = _candidate("mid", anchor="mid", snippet=None, parent_heading=None)
    decisions = await ranker.rank_batch(goal, [c_far, c_close, c_mid], _history())

    by_id = {d.candidate_id: d for d in decisions}
    assert len(decisions) == 3
    # close (sim 1.0) first, mid (0.6) second, far dropped.
    assert decisions[0].candidate_id == c_close.candidate_id
    assert decisions[1].candidate_id == c_mid.candidate_id
    assert by_id[c_far.candidate_id].dropped
    assert not by_id[c_close.candidate_id].dropped
    assert by_id[c_close.candidate_id].ranker == "embedding"
    assert by_id[c_close.candidate_id].priority == pytest.approx(1.0, abs=1e-3)
    assert "emb_sim=" in by_id[c_close.candidate_id].rationale


@pytest.mark.asyncio
async def test_empty_batch_no_embed_calls():
    embedder = _StubEmbedder({})
    ranker = EmbeddingRanker(embedder)
    decisions = await ranker.rank_batch(_goal(), [], _history())
    assert decisions == []
    assert embedder.calls == []


@pytest.mark.asyncio
async def test_goal_embedding_combines_statement_with_prompt():
    """The original prompt stays in the embedded text: the enhanced
    statement supplements it, never replaces it."""
    goal = _goal("find ml papers")
    goal.goal_statement = "I am looking for machine learning research papers"
    embedder = _StubEmbedder({})
    ranker = EmbeddingRanker(embedder, keep=10)
    await ranker.rank_batch(goal, [_candidate("k1", anchor="x", snippet=None, parent_heading=None)], _history())
    assert embedder.calls[0] == ["I am looking for machine learning research papers find ml papers"]


@pytest.mark.asyncio
async def test_goal_embedding_cached_across_batches():
    goal = _goal()
    embedder = _StubEmbedder({"a": [1.0, 0.0], "b": [0.5, 0.0]})
    ranker = EmbeddingRanker(embedder, keep=10)

    await ranker.rank_batch(goal, [_candidate("k1", anchor="a", snippet=None, parent_heading=None)], _history())
    await ranker.rank_batch(goal, [_candidate("k2", anchor="b", snippet=None, parent_heading=None)], _history())

    # Goal embedded once, not once per batch.
    goal_calls = [c for c in embedder.calls if goal.prompt in c]
    assert len(goal_calls) == 1


@pytest.mark.asyncio
async def test_mismatched_vector_count_raises():
    class _ShortEmbedder:
        async def embed(self, texts):
            return [[1.0]]

    ranker = EmbeddingRanker(_ShortEmbedder())
    with pytest.raises(RuntimeError):
        await ranker.rank_batch(_goal(), [_candidate("k1"), _candidate("k2")], _history())


# -- content hash ------------------------------------------------------


def test_content_hash_model_scoped():
    """Same text under different models must hash differently."""
    assert _content_hash("model-a", "hello") != _content_hash("model-b", "hello")
    assert _content_hash("model-a", "hello") == _content_hash("model-a", "hello")


# -- cache-aside -------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_miss_embeds_all_and_writes_back():
    embedder = _StubEmbedder({"a": [1.0, 0.0], "b": [0.0, 1.0]})
    cache = _DictCache()
    ranker = EmbeddingRanker(embedder, keep=10, cache=cache)

    await ranker.rank_batch(
        _goal(),
        [
            _candidate("k1", anchor="a", snippet=None, parent_heading=None),
            _candidate("k2", anchor="b", snippet=None, parent_heading=None),
        ],
        _history(),
    )
    # Goal embedded in one call, both candidates in another.
    assert len(embedder.calls) == 2
    assert embedder.calls[0] == [_goal().prompt]
    assert set(embedder.calls[1]) == {"a", "b"}
    # Cache got goal + candidate vectors written back.
    assert len(cache.puts) == 2
    assert sum(len(p) for p in cache.puts) == 3


@pytest.mark.asyncio
async def test_cache_hit_skips_provider():
    embedder = _StubEmbedder({"a": [1.0, 0.0]})
    cache = _DictCache()
    ranker = EmbeddingRanker(embedder, keep=10, cache=cache)

    c = _candidate("k1", anchor="a", snippet=None, parent_heading=None)
    goal = _goal()
    await ranker.rank_batch(goal, [c], _history())
    embedder.calls.clear()  # reset after warm-up

    await ranker.rank_batch(goal, [c], _history())
    # Goal is in the in-memory cache; candidate vector comes from _DictCache.
    assert embedder.calls == []


@pytest.mark.asyncio
async def test_cache_dims_mismatch_treated_as_miss():
    """Stale cache rows from a different vector space are re-embedded."""
    embedder = _StubEmbedder({"a": [1.0, 0.0, 0.0]})
    cache = _DictCache()
    ranker = EmbeddingRanker(embedder, keep=10, cache=cache)

    c = _candidate("k1", anchor="a", snippet=None, parent_heading=None)
    goal = _goal()
    await ranker.rank_batch(goal, [c], _history())  # warm: dims=3 learned, "a" cached
    embedder.calls.clear()

    # Corrupt the cached entry: 2 dims instead of 3.
    h = _content_hash("test/stub", "a")
    cache._store[h] = [1.0, 2.0]

    await ranker.rank_batch(goal, [c], _history())
    # Mismatched row is rejected and the text re-embedded.
    assert len(embedder.calls) == 1
    assert embedder.calls[0] == ["a"]
    # And the cache is healed with a fresh 3-dim vector.
    assert len(cache._store[h]) == 3


@pytest.mark.asyncio
async def test_goal_embedding_truncated():
    """Very long goals are truncated before they reach the provider."""
    embedder = _StubEmbedder({})
    ranker = EmbeddingRanker(embedder, keep=10)
    long_prompt = "x" * 1000

    await ranker.rank_batch(
        CrawlGoal(prompt=long_prompt),
        [_candidate("k1", anchor="a", snippet=None, parent_heading=None)],
        _history(),
    )
    # The goal text the provider saw is capped at 512 chars.
    assert all(len(t) <= 512 for t in embedder.calls[0])


@pytest.mark.asyncio
async def test_cache_partial_hit_embeds_only_misses():
    embedder = _StubEmbedder({"a": [1.0, 0.0], "b": [0.0, 1.0]})
    cache = _DictCache()
    ranker = EmbeddingRanker(embedder, keep=10, cache=cache)

    goal = _goal()
    c_a = _candidate("k1", anchor="a", snippet=None, parent_heading=None)
    c_b = _candidate("k2", anchor="b", snippet=None, parent_heading=None)
    await ranker.rank_batch(goal, [c_a], _history())  # warms "a"
    embedder.calls.clear()

    await ranker.rank_batch(goal, [c_a, c_b], _history())
    # Only "b" needs embedding.
    assert len(embedder.calls) == 1
    assert embedder.calls[0] == ["b"]


# -- FastEmbedEmbedder --------------------------------------------------


def test_local_embedder_model_name():
    e = FastEmbedEmbedder(model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    name = e.model_name
    assert name.startswith("local/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2@fastembed")
    # Version-scoped: different fastembed releases must not share cache entries.
    e2 = FastEmbedEmbedder(model="BAAI/bge-small-en-v1.5")
    assert e2.model_name.startswith("local/BAAI/bge-small-en-v1.5@fastembed")


def test_local_embedder_constructs_without_importing_fastembed():
    """Construction must stay lazy: no heavy import at init time."""
    e = FastEmbedEmbedder()
    assert e._fm is None


def test_local_embedder_missing_package_error(monkeypatch):
    """Without fastembed, a helpful RuntimeError is raised."""
    monkeypatch.setitem(sys.modules, "fastembed", None)
    e = FastEmbedEmbedder()
    with pytest.raises(RuntimeError, match="fastembed"):
        e._load()


def test_local_embedder_real_encode():
    """Integration: encode returns normalized vectors.

    Skips unless CRAWLME_MODEL_TEST=1, since the default model download
    (~220MB) doesn't belong in every test-suite run.
    """
    import os

    if os.environ.get("CRAWLME_MODEL_TEST") != "1":
        pytest.skip("set CRAWLME_MODEL_TEST=1 to run real model inference")
    pytest.importorskip("fastembed")
    e = FastEmbedEmbedder()
    fm = e._load()
    vecs = list(fm.embed(["hello world", "goodbye"]))
    assert len(vecs) == 2
    for v in vecs:
        assert len(v) > 0


def test_normalize_unit_vector():
    import numpy as np

    v = _normalize(np.array([3.0, 4.0]))
    assert v == pytest.approx([0.6, 0.8], abs=1e-6)


def test_normalize_zero_vector():
    import numpy as np

    v = _normalize(np.array([0.0, 0.0]))
    assert v == [0.0, 0.0]


# -- OpenAICompatibleEmbedder -------------------------------------------


@pytest.mark.asyncio
async def test_embedder_posts_and_restores_input_order():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "text-embedding-3-small"
        assert body["input"] == ["text a", "text b"]
        # API responds out of order on purpose.
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 2.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    embedder = OpenAICompatibleEmbedder(
        model="text-embedding-3-small",
        api_key="sk-test",
        transport=transport,
    )

    vecs = await embedder.embed(["text a", "text b"])
    assert vecs == [[1.0, 0.0], [0.0, 2.0]]


@pytest.mark.asyncio
async def test_embedder_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    transport = httpx.MockTransport(handler)
    embedder = OpenAICompatibleEmbedder(model="m", api_key="bad", transport=transport)

    with pytest.raises(httpx.HTTPStatusError):
        await embedder.embed(["x"])


def _counting_handler(responses: list[httpx.Response]):
    """Return a MockTransport handler that replays *responses* in order."""
    state = {"calls": 0, "inputs": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        state["inputs"].append(json.loads(request.content)["input"])
        return responses[min(state["calls"] - 1, len(responses) - 1)]

    return handler, state


# -- E3: chunking ------------------------------------------------------


@pytest.mark.asyncio
async def test_embedder_chunks_large_batches():
    """Batch larger than max_batch is split into multiple requests."""
    handler, state = _counting_handler(
        [
            httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0]}, {"index": 1, "embedding": [1.0]}]}),
            httpx.Response(200, json={"data": [{"index": 0, "embedding": [2.0]}, {"index": 1, "embedding": [3.0]}]}),
            httpx.Response(200, json={"data": [{"index": 0, "embedding": [4.0]}]}),
        ]
    )
    transport = httpx.MockTransport(handler)
    embedder = OpenAICompatibleEmbedder(model="m", api_key="k", max_batch=2, transport=transport)

    vecs = await embedder.embed(["a", "b", "c", "d", "e"])
    # Chunked into [a,b], [c,d], [e] and concatenated in order.
    assert state["inputs"] == [["a", "b"], ["c", "d"], ["e"]]
    assert vecs == [[0.0], [1.0], [2.0], [3.0], [4.0]]


@pytest.mark.asyncio
async def test_embedder_single_batch_under_limit():
    handler, state = _counting_handler([httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})])
    transport = httpx.MockTransport(handler)
    embedder = OpenAICompatibleEmbedder(model="m", api_key="k", max_batch=100, transport=transport)

    await embedder.embed(["only-one"])
    assert state["calls"] == 1


# -- E3: retries -------------------------------------------------------


@pytest.mark.asyncio
async def test_embedder_retries_transient_then_succeeds(monkeypatch):
    import crawlme.pioneer.ranker.embedding as embedding_module

    monkeypatch.setattr(embedding_module, "_EMBED_RETRY_BASE", 0.0)  # no sleeping in tests

    handler, state = _counting_handler(
        [
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(503, json={"error": "boom"}),
            httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]}),
        ]
    )
    transport = httpx.MockTransport(handler)
    embedder = OpenAICompatibleEmbedder(model="m", api_key="k", transport=transport)

    vecs = await embedder.embed(["x"])
    assert vecs == [[1.0]]
    assert state["calls"] == 3  # 1 initial + 2 retries


@pytest.mark.asyncio
async def test_embedder_retries_429(monkeypatch):
    import crawlme.pioneer.ranker.embedding as embedding_module

    monkeypatch.setattr(embedding_module, "_EMBED_RETRY_BASE", 0.0)

    handler, state = _counting_handler(
        [
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]}),
        ]
    )
    transport = httpx.MockTransport(handler)
    embedder = OpenAICompatibleEmbedder(model="m", api_key="k", transport=transport)

    await embedder.embed(["x"])
    assert state["calls"] == 2


@pytest.mark.asyncio
async def test_embedder_gives_up_after_retries(monkeypatch):
    import crawlme.pioneer.ranker.embedding as embedding_module

    monkeypatch.setattr(embedding_module, "_EMBED_RETRY_BASE", 0.0)

    handler, state = _counting_handler([httpx.Response(500, json={"error": "boom"})])
    transport = httpx.MockTransport(handler)
    embedder = OpenAICompatibleEmbedder(model="m", api_key="k", transport=transport)

    with pytest.raises(httpx.HTTPStatusError):
        await embedder.embed(["x"])
    assert state["calls"] == 3  # initial + 2 retries, then give up


@pytest.mark.asyncio
async def test_embedder_no_retry_on_permanent_4xx():
    """400 is permanent: fail immediately, no retry."""
    handler, state = _counting_handler([httpx.Response(400, json={"error": "bad request"})])
    transport = httpx.MockTransport(handler)
    embedder = OpenAICompatibleEmbedder(model="m", api_key="k", transport=transport)

    with pytest.raises(httpx.HTTPStatusError):
        await embedder.embed(["x"])
    assert state["calls"] == 1
