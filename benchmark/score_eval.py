"""Score the labeled eval set with the production rankers.

Uses the REAL RuleRanker and EmbeddingRanker (not reimplemented logic),
so the numbers measure exactly what the crawler does.

Usage:
  python3 benchmark/score_eval.py
  python3 benchmark/score_eval.py --embedding api --embedding-model text-embedding-3-small

Metrics (binary relevance):
  NDCG@k    ranking quality, discounted by position
  P@k       precision in the top k
  recall@k  share of all relevant items found in the top k
            (recall@60 mirrors the EMBEDDING_KEEP gate)
  AP        average precision over the full ranking

Per-layer sim distributions and a coarse floor-survival preview are
printed, and per-candidate raw scores (sim + rule) are dumped to
results/eval_raw_scores.json. The E5 floor/keep/blend sweeps consume
that dump, so they never re-embed the eval set.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path

from crawlme.config import Settings
from crawlme.pioneer.ranker import RuleRanker
from crawlme.pioneer.ranker.embedding import (
    Embedder,
    EmbeddingRanker,
    FastEmbedEmbedder,
    OpenAICompatibleEmbedder,
)
from crawlme.schemas import URL, Candidate, CrawlGoal, RankHistorySummary

EVAL_PATH = Path("benchmark/data/embedding_eval.json")


def _to_candidate(idx: int, e: dict[str, object]) -> Candidate:
    raw = str(e["url"])
    return Candidate(
        candidate_id=f"eval-{idx:04d}",
        url=URL(
            raw=raw,
            canonical=raw,
            url_key=hashlib.sha256(raw.encode()).hexdigest()[:16],
            reg_domain=str(e.get("domain", "")),
        ),
        anchor=str(e.get("anchor") or "") or None,
        snippet=str(e.get("snippet") or "") or None,
        parent_heading=str(e.get("parent_heading") or "") or None,
        depth=int(str(e.get("depth", 0) or 0)),
        position=1,
    )


def _dcg(rels: list[int], k: int) -> float:
    return sum(r / math.log2(i + 2) for i, r in enumerate(rels[:k]))


def _idcg(n_relevant: int, k: int) -> float:
    k = min(k, max(n_relevant, 1))
    return sum(1.0 / math.log2(i + 2) for i in range(k) if i < n_relevant)


def compute_metrics(entries: list[dict[str, object]], score_by_id: dict[str, float]) -> dict[str, float]:
    ranked = sorted(
        enumerate(entries),
        key=lambda ie: score_by_id.get(f"eval-{ie[0]:04d}", 0.0),
        reverse=True,
    )
    rels = [1 if entries[i]["relevant"] else 0 for i, _ in ranked]
    n_rel = sum(rels)
    if n_rel == 0:
        return {}

    out: dict[str, float] = {}
    for k in (5, 10, 50):
        dcg, idcg = _dcg(rels, k), _idcg(n_rel, k)
        out[f"NDCG@{k}"] = round(dcg / idcg, 4) if idcg > 0 else 0.0
    for k in (5, 10):
        out[f"P@{k}"] = round(sum(rels[:k]) / k, 4)
    for k in (60, 100):
        out[f"recall@{k}"] = round(sum(rels[:k]) / n_rel, 4)
    # Average precision over the full ranking.
    hits = 0
    ap = 0.0
    for i, r in enumerate(rels):
        if r:
            hits += 1
            ap += hits / (i + 1)
    out["AP"] = round(ap / n_rel, 4)
    return out


def _percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile of a non-empty list."""
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _layer_distribution(entries: list[dict[str, object]], scores: dict[str, float]) -> None:
    """Per-layer sim percentiles. Do the noise and hard layers separate?"""
    by_layer: dict[str, list[float]] = {}
    for i, e in enumerate(entries):
        by_layer.setdefault(str(e["layer"]), []).append(scores.get(f"eval-{i:04d}", 0.0))
    print(f"\n{'layer':18} {'n':>4} {'p5':>6} {'p25':>6} {'p50':>6} {'p75':>6} {'p90':>6} {'max':>6}")
    for layer in sorted(by_layer):
        vals = by_layer[layer]
        row = " ".join(f"{_percentile(vals, p):6.3f}" for p in (5, 25, 50, 75, 90))
        print(f"{layer:18} {len(vals):4d} {row} {max(vals):6.3f}")


def _floor_preview(entries: list[dict[str, object]], scores: dict[str, float]) -> None:
    """Fraction of each layer surviving a raw-sim floor, coarse sweep."""
    layers = sorted({str(e["layer"]) for e in entries})
    header = " ".join(f"{name:>16}" for name in layers)
    print(f"\nfloor survival by layer:\n{'floor':>6} {header}")
    for f in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45):
        hits = {name: 0 for name in layers}
        totals = {name: 0 for name in layers}
        for i, e in enumerate(entries):
            name = str(e["layer"])
            totals[name] += 1
            if scores.get(f"eval-{i:04d}", 0.0) >= f:
                hits[name] += 1
        row = " ".join(f"{hits[name] / totals[name]:16.3f}" for name in layers)
        print(f"{f:6.2f} {row}")


async def _score_rule(goal: CrawlGoal, candidates: list[Candidate]) -> dict[str, float]:
    ranker = RuleRanker(threshold=0.0)  # no gate: we want scores for everyone
    decisions = await ranker.rank_batch(goal, candidates, RankHistorySummary())
    return {d.candidate_id: d.priority for d in decisions}


async def _score_embedding(
    goal: CrawlGoal,
    candidates: list[Candidate],
    provider: str,
    model: str | None,
) -> dict[str, float]:
    settings = Settings()
    embedder: Embedder
    if provider == "api":
        embedder = OpenAICompatibleEmbedder(
            model=model or "text-embedding-3-small",
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
        )
    else:
        embedder = FastEmbedEmbedder(model=model or "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    ranker = EmbeddingRanker(embedder, keep=len(candidates))  # no gate
    decisions = await ranker.rank_batch(goal, candidates, RankHistorySummary())
    return {d.candidate_id: d.priority for d in decisions}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedding", choices=["local", "api"], default="local")
    parser.add_argument("--embedding-model", default=None)
    args = parser.parse_args()

    data = json.loads(EVAL_PATH.read_text())
    goal_prompt = data["goal"]["prompt"]
    entries: list[dict[str, object]] = data["goal"]["candidates"]
    goal = CrawlGoal(prompt=goal_prompt)
    candidates = [_to_candidate(i, e) for i, e in enumerate(entries)]
    n_rel = sum(1 for e in entries if e["relevant"])
    print(f"eval set: {len(entries)} candidates, {n_rel} relevant, goal: {goal_prompt!r}\n")

    rule_scores = await _score_rule(goal, candidates)
    rule_metrics = compute_metrics(entries, rule_scores)

    emb_scores = await _score_embedding(goal, candidates, args.embedding, args.embedding_model)
    emb_metrics = compute_metrics(entries, emb_scores)

    keys = [k for k in rule_metrics if k in emb_metrics]
    print(f"{'metric':12} {'rule-only':>10} {'embedding':>10}")
    print("-" * 34)
    for k in keys:
        print(f"{k:12} {rule_metrics[k]:>10} {emb_metrics[k]:>10}")

    print("\nper-layer sim distribution (embedding):")
    _layer_distribution(entries, emb_scores)
    _floor_preview(entries, emb_scores)

    # Raw scores for the offline sweeps. E5 floor/keep/blend runs consume
    # this dump, so re-scoring never re-embeds the eval set.
    raw = [
        {
            "idx": i,
            "layer": e["layer"],
            "relevant": e["relevant"],
            "sim": round(emb_scores.get(f"eval-{i:04d}", 0.0), 6),
            "rule": round(rule_scores.get(f"eval-{i:04d}", 0.0), 6),
        }
        for i, e in enumerate(entries)
    ]
    raw_out = Path("results") / "eval_raw_scores.json"
    raw_out.parent.mkdir(parents=True, exist_ok=True)
    raw_out.write_text(json.dumps(raw, indent=2))
    print(f"\nraw scores written: {raw_out}")

    result = {
        "eval_set": str(EVAL_PATH),
        "embedding_config": {"provider": args.embedding, "model": args.embedding_model},
        "rule_only": rule_metrics,
        "embedding": emb_metrics,
    }
    out = Path("results") / "eval_scores.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(f"\nscores written: {out}")


if __name__ == "__main__":
    asyncio.run(main())
