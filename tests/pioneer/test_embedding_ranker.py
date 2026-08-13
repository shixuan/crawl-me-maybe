from __future__ import annotations

import json

import httpx
import pytest

from crawlme.pioneer.ranker.embedding import EmbeddingRanker, OpenAICompatibleEmbedder, _cosine, _text_for
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

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vectors.get(t, self._goal_vector) for t in texts]


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
