"""Offline sweep of the ranking knobs on the labeled eval set.

Consumes results/eval_raw_scores.json (written by score_eval.py) and
re-scores every (floor, keep) and (blend weight, floor) combination
without re-embedding. Each row reports what survives into the top
keep slots, how much of each layer that is, and the ranking metrics
of the surviving list.

Usage: python3 benchmark/embedding/sweep_params.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from score_eval import compute_metrics

RAW_PATH = Path("results/eval_raw_scores.json")

_LAYERS = (
    "semantic_hard",
    "unrelated_wiki",
    "trap_external",
    "off_topic_branch",
    "on_topic_wiki",
)


def _load() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], json.loads(RAW_PATH.read_text()))


def _row(raw: list[dict[str, Any]], scored: list[tuple[int, float]], keep: int) -> str:
    kept = scored[:keep]
    by_idx = {int(r["idx"]): r for r in raw}
    entries = [{"relevant": by_idx[i]["relevant"], "layer": by_idx[i]["layer"]} for i, _ in kept]
    scores = {f"eval-{i:04d}": s for i, s in kept}
    m = compute_metrics(entries, scores)
    n_rel_all = sum(1 for r in raw if r["relevant"])
    rel_kept = sum(1 for i, _ in kept if by_idx[i]["relevant"])
    counts = [sum(1 for i, _ in kept if by_idx[i]["layer"] == lyr) for lyr in _LAYERS]
    comp = " ".join(f"{c:3d}" for c in counts)
    return f"{len(kept):3d} {rel_kept / n_rel_all:8.3f} {comp} {m.get('NDCG@50', 0.0):8.3f} {m.get('AP', 0.0):8.3f}"


def main() -> None:
    raw = _load()
    header = (
        f"{'floor':>5} {'keep':>4} {'n':>3} {'rel_rec':>8} "
        + " ".join(f"{lyr[:6]:>6}" for lyr in _LAYERS)
        + f" {'NDCG@50':>8} {'AP':>8}"
    )
    print(f"sim only (no blend):\n{header}")
    for floor in (0.0, 0.20, 0.25, 0.30, 0.35):
        scored = [(r["idx"], r["sim"]) for r in raw if r["sim"] >= floor]
        scored.sort(key=lambda p: p[1], reverse=True)
        for keep in (60, 100, 150):
            print(f"{floor:5.2f} {keep:4d} " + _row(raw, scored, keep))

    print(f"\nblend w*sim + (1-w)*rule, keep=60:\n{header}")
    for w in (0.6, 0.7, 0.8, 0.9):
        scored = [(r["idx"], w * r["sim"] + (1 - w) * r["rule"]) for r in raw]
        scored.sort(key=lambda p: p[1], reverse=True)
        for floor in (0.0, 0.20, 0.25, 0.30, 0.35):
            kept = [(i, s) for i, s in scored if s >= floor]
            print(f"{floor:5.2f} {'w=' + str(w):>4} " + _row(raw, kept, 60))


if __name__ == "__main__":
    main()
