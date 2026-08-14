"""EmbeddingRanker: semantic similarity scoring, zero LLM cost.

The second stage of the ranking funnel (v0.1.1).  While RuleRanker
matches surface words, EmbeddingRanker compares meaning: goal and
candidate texts are embedded into vectors and ranked by cosine
similarity, so candidates that share no words with the goal but mean
the same thing still score well.

    goal embedding     : computed once per task, cached by goal_id
    candidate embedding: anchor + snippet + parent_heading + source
                         page title, batched into one provider call

Vectors are persisted through an optional EmbeddingCache keyed by
content hash (sha256 of model + text).  Vectors from different models
are never mixed: the model name is part of the hash.

Selection: candidates are ranked by similarity and only the top
*keep* survive; the rest are marked dropped.  Priority is the raw
cosine similarity in [0, 1].

Providers (both implement Embedder):
  - FastEmbedEmbedder       : local ONNX model via fastembed, zero API
                              cost, no torch dependency
  - OpenAICompatibleEmbedder: any OpenAI-compatible /embeddings
                              endpoint (OpenAI, Jina, self-hosted)
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import math
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any, Protocol

import httpx

from crawlme.schemas import Candidate, CrawlGoal, RankDecision, RankHistorySummary

logger = logging.getLogger(__name__)

_EMBED_TIMEOUT = 30.0
# Transient failures (timeout, 429, 5xx) get this many retries with
# exponential backoff before the HybridRanker falls back to rule scores.
_EMBED_MAX_RETRIES = 2
_EMBED_RETRY_BASE = 0.5  # seconds; doubled per attempt
# Safety valve: very long texts (e.g. a verbose goal) are truncated so
# providers don't hit token limits.  512 chars is a balance between the
# 128-token window of small local models and keeping most of the signal.
_MAX_EMBED_CHARS = 512


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _content_hash(model: str, text: str) -> str:
    """Model-scoped content fingerprint: same text under a different
    model is a different hash, so vectors are never compared across
    incompatible models."""
    return hashlib.sha256(f"{model}\x00{text}".encode()).hexdigest()


class Embedder(Protocol):
    """Contract for embedding providers: batch of texts -> vectors.

    *model_name* identifies the model (and thus the vector space);
    EmbeddingRanker uses it for cache scoping.
    """

    @property
    def model_name(self) -> str: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingCache(Protocol):
    """Contract for persistent vector storage."""

    async def get_vectors(self, content_hashes: list[str]) -> dict[str, list[float]]: ...

    async def put_vectors(self, entries: list[tuple[str, list[float]]], model: str) -> None: ...

    async def close(self) -> None: ...


class FastEmbedEmbedder:
    """Local embedding via fastembed (ONNX runtime, no torch).

    The model is loaded lazily on first use: constructing this class
    imports nothing heavy, and the model weights download (and cache)
    on the first embed() call.  Inference runs in a worker thread;
    vectors are L2-normalized so cosine == dot product.

    fastembed ships as a core dependency, so this works out of the box.
    """

    def __init__(
        self,
        model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        max_batch: int | None = None,
    ) -> None:
        self._model = model
        self._max_batch = max_batch
        self._fm: Any | None = None

    @property
    def model_name(self) -> str:
        # The fastembed version is part of the cache identity: pooling
        # strategies and ONNX artifacts change between releases, so
        # vectors from different versions must never share a cache key.
        try:
            v = _pkg_version("fastembed")
        except PackageNotFoundError:
            v = "unknown"
        return f"local/{self._model}@fastembed{v}"

    def _load(self) -> Any:
        if self._fm is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as e:
                raise RuntimeError(
                    "local embedding requires the 'fastembed' package, which ships as a "
                    "core dependency: reinstall with `pip install -e .`"
                ) from e
            logger.info(
                "embed.local.load model=%s (first use downloads the model if not cached)",
                self._model,
            )
            self._fm = TextEmbedding(model_name=self._model)
        return self._fm

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        fm = self._load()

        async def _run(chunk: list[str]) -> list[list[float]]:
            vecs = await asyncio.to_thread(lambda: list(fm.embed(chunk)))
            return [_normalize(v) for v in vecs]

        return await _chunk(texts, self._max_batch, _run)


def _normalize(vec: Any) -> list[float]:
    """L2-normalize a numpy vector and convert to a plain list."""
    v = vec.astype(float)
    norm = float((v @ v) ** 0.5)
    if norm > 0:
        v = v / norm
    return list(v.tolist())


def _truncate(text: str) -> str:
    return text[:_MAX_EMBED_CHARS]


async def _chunk(
    texts: list[str],
    max_batch: int | None,
    fn: Callable[[list[str]], Awaitable[list[list[float]]]],
) -> list[list[float]]:
    """Split *texts* into provider-sized batches and concatenate in order."""
    if max_batch is None or len(texts) <= max_batch:
        return await fn(texts)
    out: list[list[float]] = []
    for i in range(0, len(texts), max_batch):
        out.extend(await fn(texts[i : i + max_batch]))
    return out


class OpenAICompatibleEmbedder:
    """POST {base_url}/embeddings (OpenAI, Jina, and most other providers).

    Defaults to OpenAI's endpoint.  Pass *base_url* to point at another
    OpenAI-compatible provider (e.g. https://api.jina.ai/v1); *api_key*
    is omitted from the headers when empty (local endpoints).

    Batches larger than *max_batch* are split into multiple requests.
    Transient failures (timeout, connect errors, 429, 5xx) retry
    _EMBED_MAX_RETRIES times with exponential backoff; permanent 4xx
    errors raise immediately.
    """

    def __init__(
        self,
        model: str,
        api_key: str = "",
        base_url: str = "",
        max_batch: int | None = 100,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._max_batch = max_batch
        self._transport = transport

    @property
    def model_name(self) -> str:
        return f"api/{self._model}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        last_err: BaseException | None = None
        for attempt in range(_EMBED_MAX_RETRIES + 1):
            try:
                return await _chunk(texts, self._max_batch, self._post_embed)
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_err = e
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status == 429 or status >= 500:
                    last_err = e
                else:
                    raise
            if attempt < _EMBED_MAX_RETRIES:
                delay = _EMBED_RETRY_BASE * (2**attempt)
                logger.warning(
                    "embed.retry attempt=%d/%d delay=%.1fs error=%s",
                    attempt + 1,
                    _EMBED_MAX_RETRIES,
                    delay,
                    last_err,
                )
                await asyncio.sleep(delay)
        assert last_err is not None  # loop only ends by exhausting retries
        raise last_err

    async def _post_embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with httpx.AsyncClient(timeout=_EMBED_TIMEOUT, transport=self._transport) as client:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                headers=headers,
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
        # API may return vectors out of order; restore input order by index.
        rows = sorted(data["data"], key=lambda row: row["index"])
        return [row["embedding"] for row in rows]


class EmbeddingRanker:
    """Rank candidates by semantic similarity to the goal.

    Candidates scoring below the top-*keep* are dropped.  On provider
    failure the exception propagates: HybridRanker catches it and falls
    back to the rule stage, so a dead embedding provider never blocks
    the pipeline.

    When *cache* is provided, vectors are persisted model-scoped by
    content hash: repeated texts (same candidates re-ranked, replay,
    new tasks) skip the provider entirely.
    """

    def __init__(self, embedder: Embedder, keep: int = 60, cache: EmbeddingCache | None = None) -> None:
        self._embedder = embedder
        self._keep = keep
        self._cache = cache
        self._goal_cache: dict[str, list[float]] = {}
        # Vector dimensionality learned from the first provider response;
        # used to reject cache entries from an incompatible vector space.
        self._dims: int | None = None

    async def rank_batch(
        self,
        goal: CrawlGoal,
        candidates: list[Candidate],
        history: RankHistorySummary,
        page_contexts: dict[str, dict[str, Any]] | None = None,
    ) -> list[RankDecision]:
        if not candidates:
            return []

        goal_emb = await self._goal_embedding(goal)
        texts = [_text_for(c, page_contexts) for c in candidates]
        embs = await self._embed_texts(texts)
        if len(embs) != len(candidates):
            raise RuntimeError(f"embedder returned {len(embs)} vectors for {len(candidates)} texts")

        scored = sorted(
            ((_cosine(goal_emb, emb), c) for c, emb in zip(candidates, embs)),
            key=lambda pair: pair[0],
            reverse=True,
        )

        decisions: list[RankDecision] = []
        for i, (sim, c) in enumerate(scored):
            decisions.append(
                RankDecision(
                    candidate_id=c.candidate_id,
                    url_key=c.url.url_key,
                    priority=round(sim, 4),
                    dropped=i >= self._keep,
                    ranker="embedding",
                    rationale=f"emb_sim={sim:.4f}",
                    decided_at=_utcnow(),
                )
            )
        return decisions

    async def aclose(self) -> None:
        """Close the persistent vector cache when present.

        The cache owns an aiosqlite connection whose worker thread
        would otherwise keep the interpreter alive at exit.
        """
        if self._cache is not None:
            await self._cache.close()

    async def _goal_embedding(self, goal: CrawlGoal) -> list[float]:
        """Embed the goal once per task; in-memory cache by goal_id.

        The persistent cache (if wired) also applies: the goal text is
        hashed like any other text, so a re-run of the same goal under
        the same model reuses the stored vector.
        """
        cached = self._goal_cache.get(goal.goal_id)
        if cached is not None:
            return cached
        # The original prompt always stays in the embedded text: the
        # statement supplements it, it never replaces it.
        text = f"{goal.goal_statement} {goal.prompt}" if goal.goal_statement else goal.prompt
        emb = (await self._embed_texts([_truncate(text)]))[0]
        self._goal_cache[goal.goal_id] = emb
        return emb

    async def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Cache-aside embedding: hit the cache first, embed only misses."""
        if self._cache is None:
            return await self._embedder.embed(texts)

        model = self._embedder.model_name
        hashes = [_content_hash(model, t) for t in texts]
        cached = await self._cache.get_vectors(hashes)

        # Reject cache entries whose dimensionality doesn't match the
        # live provider (stale rows from a different vector space).
        if self._dims is not None:
            for h, v in list(cached.items()):
                if len(v) != self._dims:
                    logger.warning("embed.cache.dims_mismatch hash=%s cached=%d expected=%d", h, len(v), self._dims)
                    del cached[h]

        miss = [(i, t) for i, t in enumerate(texts) if hashes[i] not in cached]
        new_by_hash: dict[str, list[float]] = {}
        if miss:
            new_vecs = await self._embedder.embed([t for _, t in miss])
            if new_vecs:
                self._dims = len(new_vecs[0])
            entries = [(hashes[i], v) for (i, _), v in zip(miss, new_vecs)]
            await self._cache.put_vectors(entries, model)
            new_by_hash = {h: v for h, v in entries}
        if hashes:
            logger.debug("embed.cache total=%d hit=%d miss=%d", len(texts), len(texts) - len(miss), len(miss))
        return [cached[h] if h in cached else new_by_hash[h] for h in hashes]


def _text_for(c: Candidate, page_contexts: dict[str, dict[str, Any]] | None) -> str:
    """Compose the candidate text to embed: link context + source title."""
    parts = [c.anchor, c.snippet, c.parent_heading]
    if page_contexts:
        parts.append(page_contexts.get(c.source_url_key or "", {}).get("title", ""))
    text = " ".join(p for p in parts if p).strip()
    return _truncate(text) if text else c.url.canonical[:_MAX_EMBED_CHARS]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
