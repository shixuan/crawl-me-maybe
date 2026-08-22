"""Score a finished run's candidates the way the embedding stage would.

Offline and repeatable: the candidate text and the analyzer's verdict are
already in the run database, so a setting can be swept without crawling
again or spending a token.

    python benchmark/embedding/score.py results/20260821_234041
    python benchmark/embedding/score.py results/<id> --max-tokens 512

The run must have been made with --recall, or this scores survivors and
every ranker looks equally good on those.  See README.md.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from crawlme.pioneer.ranker.rule import GRAPH_FACTORS, ScoreContext, _score_one, factors_for
from crawlme.schemas import URL, Candidate


@dataclass
class Row:
    """One candidate, with what it carried and what it turned out to be."""

    text: str
    url: str
    anchor: str
    relevant: bool


def load(run: Path) -> tuple[str, list[str], list[Row]]:
    """Every candidate the run both ranked and later judged.

    The keywords come back too, and they matter: the rule ranker scores
    on the ones the goal enhancer curated, phrases included.  Splitting
    the prompt on spaces instead feeds it "with" and "the" and drops
    every phrase, which is a measurement of the benchmark rather than of
    the ranker.
    """
    con = sqlite3.connect(f"file:{run / 'db' / 'crawl.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    goal = con.execute("SELECT prompt, goal_statement, keywords FROM crawl_goals LIMIT 1").fetchone()
    goal_text = f"{goal['goal_statement']} {goal['prompt']}".strip() if goal else ""
    keywords = json.loads(goal["keywords"] or "[]") if goal else []
    rows = [
        Row(
            text=r["text"] or "",
            url=json.loads(r["url_json"]).get("canonical", ""),
            anchor=r["anchor"] or "",
            relevant=r["classification"] == "RELEVANT",
        )
        for r in con.execute(
            "SELECT l.text, l.url_json, l.anchor, a.classification "
            "FROM links l JOIN analyses a ON a.url_key = l.url_key"
        )
    ]
    con.close()
    return goal_text, keywords, rows


def rule_scores(keywords: list[str], rows: list[Row]) -> list[float]:
    """What the rule ranker alone makes of them: the baseline to beat."""
    ctx = ScoreContext(goal_keywords=list(keywords))
    out = []
    for r in rows:
        c = _as_candidate(r)
        priority, _ = _score_one(c, ctx, factors_for(c) or GRAPH_FACTORS)
        out.append(priority)
    return out


def embed_scores(goal_text: str, rows: list[Row], max_tokens: int | None) -> list[float]:
    """Cosine to the goal, at a chosen truncation.

    *max_tokens* None leaves the model packaged as it ships, which is the
    number the crawler actually runs with.
    """
    warnings.filterwarnings("ignore")
    from fastembed import TextEmbedding

    model = TextEmbedding("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    if max_tokens is not None:
        model.model.tokenizer.enable_truncation(max_length=max_tokens)
    goal_vec = next(iter(model.embed([goal_text])))
    vecs = list(model.embed([r.text or r.anchor or r.url for r in rows]))
    return [_cosine(goal_vec, v) for v in vecs]


def report(name: str, scores: list[float], rows: list[Row], cuts: tuple[int, ...]) -> None:
    order = sorted(range(len(rows)), key=lambda i: -scores[i])
    labels = [rows[i].relevant for i in order]
    total = sum(labels)
    print(f"\n{name}")
    print(f"  AP {_average_precision(labels):.3f}")
    for k in cuts:
        if k > len(labels):
            continue
        kept = labels[:k]
        # The trade in one line: cutting here saves the fetches below it
        # and costs the relevant ones that were down there.
        lost, saved = total - sum(kept), len(labels) - k
        print(
            f"  @{k:<4} precision {sum(kept) / k:.3f}  recall {sum(kept) / max(total, 1):.3f}"
            f"  |  {saved} fetches saved, {lost} relevant lost"
        )


def _as_candidate(r: Row) -> Candidate:
    return Candidate(url=URL(raw=r.url, canonical=r.url, url_key=r.url), text=r.text, anchor=r.anchor or None)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _average_precision(labels: list[bool]) -> float:
    hits, total = 0, 0.0
    for i, rel in enumerate(labels, start=1):
        if rel:
            hits += 1
            total += hits / i
    return total / hits if hits else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run", type=Path, help="A run directory under results/")
    ap.add_argument("--max-tokens", type=int, default=None, help="Truncation to try (default: as shipped)")
    ap.add_argument("--cuts", default="10,20,40,60", help="Where to imagine cutting the ranking")
    args = ap.parse_args()

    goal_text, keywords, rows = load(args.run)
    if not rows:
        print("no candidates with a verdict: was this run made with --recall?", file=sys.stderr)
        return 1
    cuts = tuple(int(c) for c in args.cuts.split(",") if c.strip())
    relevant = sum(r.relevant for r in rows)
    print(f"{len(rows)} candidates, {relevant} relevant ({relevant / len(rows):.0%})")
    print(f"goal: {goal_text[:80]}")
    print(f"keywords: {', '.join(keywords) or '(none)'}")
    if relevant / len(rows) > 0.7:
        print("\n! base rate above 70%: this looks like survivors, not a --recall run.")
        print("! every ranker scores well on candidates something else already kept.")

    rule = rule_scores(keywords, rows)
    label = "as shipped (128 tokens)" if args.max_tokens is None else f"{args.max_tokens} tokens"
    emb = embed_scores(goal_text, rows, args.max_tokens)

    # Split by whether the candidate brought its own text, because that
    # decides which factor set the rule ranker used and therefore what
    # is being compared.  Mixed together, one measurement said the rule
    # ranker was useless on feeds; separated, it scored 0.884 on feed
    # entries and the 0.134 belonged to the ordinary links around them.
    for name, keep in (("candidates carrying their own text", True), ("ordinary links, no text", False)):
        idx = [i for i, r in enumerate(rows) if bool(r.text) == keep]
        if not idx:
            continue
        sub = [rows[i] for i in idx]
        relevant = sum(r.relevant for r in sub)
        print(f"\n———— {name}: {len(sub)} candidates, {relevant} relevant ({relevant / len(sub):.0%})")
        report("  rule only", [rule[i] for i in idx], sub, cuts)
        report(f"  embedding, {label}", [emb[i] for i in idx], sub, cuts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
