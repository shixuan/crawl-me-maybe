"""EmbeddingRanker: semantic similarity scoring, zero LLM cost.

The second stage of the ranking funnel (v0.1.1).  While RuleRanker
matches surface words, EmbeddingRanker compares meaning: goal and
candidate texts are embedded into vectors and ranked by cosine
similarity, so candidates that share no words with the goal but mean
the same thing still score well.

    goal embedding     : computed once per task, cached by goal_id
    candidate embedding: anchor + snippet + parent_heading + source
                         page title, batched into one API call

Selection: candidates are ranked by similarity and only the top
*keep* survive; the rest are marked dropped.  Priority is the raw
cosine similarity in [0, 1].

Providers: any OpenAI-compatible /embeddings endpoint works (OpenAI,
Jina, Ollama, self-hosted).  See OpenAICompatibleEmbedder below.
"""

from __future__ import annotations

import datetime
import math
from typing import Any, Protocol

import httpx

from crawlme.schemas import Candidate, CrawlGoal, RankDecision, RankHistorySummary

_EMBED_TIMEOUT = 30.0


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Embedder(Protocol):
    """Contract for embedding providers: batch of texts -> vectors."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatibleEmbedder:
    """POST {base_url}/embeddings — OpenAI, Jina, and most other providers.

    Defaults to OpenAI's endpoint.  Pass *base_url* to point at another
    OpenAI-compatible provider (e.g. https://api.jina.ai/v1); *api_key*
    is omitted from the headers when empty (local endpoints).
    """

    def __init__(
        self,
        model: str,
        api_key: str = "",
        base_url: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._transport = transport

    async def embed(self, texts: list[str]) -> list[list[float]]:
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
    back to the rule stage, so a dead embedding API never blocks the
    pipeline.
    """

    def __init__(self, embedder: Embedder, keep: int = 60) -> None:
        self._embedder = embedder
        self._keep = keep
        self._goal_cache: dict[str, list[float]] = {}

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
        embs = await self._embedder.embed(texts)
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

    async def _goal_embedding(self, goal: CrawlGoal) -> list[float]:
        """Embed the goal once per task and cache by goal_id."""
        cached = self._goal_cache.get(goal.goal_id)
        if cached is not None:
            return cached
        text = goal.goal_statement or goal.prompt
        emb = (await self._embedder.embed([text]))[0]
        self._goal_cache[goal.goal_id] = emb
        return emb


def _text_for(c: Candidate, page_contexts: dict[str, dict[str, Any]] | None) -> str:
    """Compose the candidate text to embed: link context + source title."""
    parts = [c.anchor, c.snippet, c.parent_heading]
    if page_contexts:
        parts.append(page_contexts.get(c.source_url_key or "", {}).get("title", ""))
    text = " ".join(p for p in parts if p).strip()
    return text or c.url.raw


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
