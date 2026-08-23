"""Is the thinking worth what it costs?

On one measured crawl, 84% of every output token was the model thinking
before it answered.  Thinking is billed as output, at output prices, and
then discarded -- only the JSON after it is ever read.  That made it
roughly three quarters of the run's bill.

This re-ranks a finished run's candidates twice, through the ranker the
crawler actually ships, once with thinking on and once with it off, and
scores both against the verdicts the analyzer reached after reading the
pages.  What comes out is the trade in one line: what the thinking buys
in ranking quality, and what it costs in tokens.

    python benchmark/reasoning/ab.py results/<run-id>

It spends tokens -- about twice what the run's own ranking stage spent,
since it ranks everything twice.  Nothing else about the run is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from crawlme.config import Settings
from crawlme.llm import LLMClient, TokenBudget, close_litellm_clients
from crawlme.pioneer.ranker import LLMRanker
from crawlme.schemas import URL, Candidate, CrawlGoal, RankHistorySummary


@dataclass
class Row:
    url: str
    text: str
    anchor: str
    relevant: bool


def load(run: Path) -> tuple[CrawlGoal, list[Row]]:
    """The candidates the run both ranked and later judged.

    One row per candidate, not per discovery: links records every time a
    candidate was found, and scoring a candidate four times because four
    pages linked to it measures the crawl's shape, not the ranker.
    """
    con = sqlite3.connect(f"file:{run / 'db' / 'crawl.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    g = con.execute("SELECT prompt, goal_statement, keywords FROM crawl_goals LIMIT 1").fetchone()
    goal = CrawlGoal(
        prompt=g["prompt"],
        goal_statement=g["goal_statement"] or "",
        keywords=json.loads(g["keywords"] or "[]"),
    )
    rows = [
        Row(
            url=json.loads(r["url_json"]).get("canonical", ""),
            text=r["text"] or "",
            anchor=r["anchor"] or "",
            relevant=r["classification"] == "RELEVANT",
        )
        for r in con.execute(
            "SELECT l.url_json, l.text, l.anchor, a.classification "
            "FROM links l JOIN analyses a ON a.url_key = l.url_key GROUP BY l.url_key"
        )
    ]
    con.close()
    return goal, rows


def _candidates(rows: list[Row]) -> list[Candidate]:
    return [
        Candidate(
            url=URL(raw=r.url, canonical=r.url, url_key=r.url),
            text=r.text or None,
            anchor=r.anchor or None,
            depth=1,
            position=i,
        )
        for i, r in enumerate(rows)
    ]


async def rank_all(goal: CrawlGoal, rows: list[Row], effort: str, batch: int) -> tuple[dict[str, float], int, int]:
    """Score every candidate through the shipped ranker.

    *effort* goes to the provider untouched; "" is whatever it does by
    default, which is what every run so far has paid for.
    """
    cfg = Settings()
    budget = TokenBudget(limit=0)
    client = LLMClient(
        cfg.llm_model,
        api_key=cfg.llm_api_key,
        base_url=cfg.llm_base_url,
        concurrency=cfg.llm_concurrency,
        budget=budget,
        max_output_tokens=cfg.llm_max_output_tokens,
        reasoning_effort=effort,
    )
    ranker = LLMRanker(client, max_batch_chars=cfg.llm_max_batch_chars)
    cands = _candidates(rows)
    scores: dict[str, float] = {}
    for i in range(0, len(cands), batch):
        chunk = cands[i : i + batch]
        decisions = await ranker.rank_batch(goal, chunk, RankHistorySummary())
        for d in decisions:
            scores[d.url_key] = 0.0 if d.dropped else d.priority
        print(f"    batch {i // batch + 1}: {len(chunk)} candidates, {budget.used} tokens so far", flush=True)
    await ranker.aclose()
    return scores, budget.output_tokens, budget.reasoning_tokens


def auc(scores: list[float], labels: list[bool]) -> float:
    """Chance a random relevant candidate outranks a random irrelevant
    one.  0.5 is a coin flip; ties count as half."""
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return float("nan")
    wins = sum((a > b) + 0.5 * (a == b) for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run", type=Path, help="A finished run directory under results/")
    ap.add_argument("--batch", type=int, default=20, help="Candidates per ranking call (engine uses 20)")
    ap.add_argument("--off", default="none", help="The value that disables thinking on this provider")
    args = ap.parse_args()

    goal, rows = load(args.run)
    if not rows:
        print("no candidate has both a ranking and a verdict: was this run made with --recall?", file=sys.stderr)
        return 1
    labels = [r.relevant for r in rows]
    print(f"{len(rows)} candidates, {sum(labels)} relevant ({sum(labels) / len(rows):.0%})")
    print(f"goal: {(goal.goal_statement or goal.prompt)[:70]}\n")

    results = []
    for name, effort in (("thinking on (as it ships)", ""), (f"thinking off ({args.off})", args.off)):
        print(f"  {name}")
        scores, out, think = await rank_all(goal, rows, effort, args.batch)
        ordered = [scores.get(r.url, 0.0) for r in rows]
        a = auc(ordered, labels)
        results.append((name, a, out, think))
        # Printed here, not only in the table: each pass costs tokens,
        # and a crash in the second one should not throw away the first.
        print(f"    -> AUC {a:.3f}, {out:,} output tokens, {think:,} of them thinking\n", flush=True)

    print(f"{'':28}{'AUC':>8}{'output':>10}{'thinking':>10}")
    for name, a, out, think in results:
        print(f"  {name:26}{a:>8.3f}{out:>10,}{think:>10,}")
    (_, a_on, out_on, _), (_, a_off, out_off, _) = results
    print(f"\n  thinking bought {a_on - a_off:+.3f} AUC for {out_on - out_off:+,} output tokens")
    # litellm keeps async clients alive past the last call; without this
    # the loop closes under them and the exit prints a wall of noise.
    await close_litellm_clients()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
