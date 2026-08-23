"""Does the analyzer's thinking buy anything the run keeps?

The ranking stage could be judged against the analyzer.  The analyzer
cannot: it is the standard everything else is measured by, so there is
nothing above it to appeal to.  What it does have is a check on itself.
Every field it extracts carries a quote, and a quote that is not in the
page text gets the field thrown away.  That check is a hallucination
detector already wired in, and it costs nothing to read.

So this re-analyses pages a finished run already fetched, twice, with
thinking on and off, and compares three things the run actually cares
about:

  fields kept      -- verified extractions are the product; fewer is worse
  evidence thrown  -- quotes that were not in the page; more is worse
  agreement        -- how often the two configurations reach the same verdict

    python benchmark/reasoning/analyzer_ab.py results/<run-id> --limit 60

It spends tokens: two analyses per page, at roughly what the run paid
per page.  Nothing is written back to the run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from crawlme.analyzer import PageAnalyzer
from crawlme.config import Settings
from crawlme.llm import LLMClient, TokenBudget, close_litellm_clients
from crawlme.schemas import URL, CrawlGoal, Page


class _RejectionCounter(logging.Handler):
    """Counts the fields the evidence check threw away.

    The analyzer logs one line per rejected field and keeps no tally, so
    the log is where the number lives.  Reading it here rather than
    changing the analyzer keeps the thing under test unmodified.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.rejected = 0

    def emit(self, record: logging.LogRecord) -> None:
        if "evidence_not_found" in record.getMessage():
            self.rejected += 1


def load(run: Path, limit: int) -> tuple[CrawlGoal, list[Page]]:
    con = sqlite3.connect(f"file:{run / 'db' / 'crawl.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    g = con.execute("SELECT prompt, goal_statement, keywords, extraction_spec FROM crawl_goals LIMIT 1").fetchone()
    goal = CrawlGoal(
        prompt=g["prompt"],
        goal_statement=g["goal_statement"] or "",
        keywords=json.loads(g["keywords"] or "[]"),
        extraction_spec=json.loads(g["extraction_spec"] or "null"),
    )
    pages = []
    for r in con.execute(
        "SELECT page_id, url_key, url_json, title, plain_text FROM pages "
        "WHERE plain_text IS NOT NULL AND LENGTH(plain_text) > 200 LIMIT ?",
        (limit,),
    ):
        u = json.loads(r["url_json"])
        pages.append(
            Page(
                page_id=r["page_id"],
                url_key=r["url_key"],
                url=URL(raw=u.get("canonical", ""), canonical=u.get("canonical", ""), url_key=r["url_key"]),
                title=r["title"] or "",
                plain_text=r["plain_text"],
            )
        )
    con.close()
    return goal, pages


async def analyse_all(goal: CrawlGoal, pages: list[Page], effort: str) -> dict[str, object]:
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
    analyzer = PageAnalyzer(client, max_page_chars=cfg.analyzer_max_chars)
    counter = _RejectionCounter()
    logging.getLogger("crawlme.analyzer.page_analyzer").addHandler(counter)

    detail: dict[str, dict] = {}
    kept = 0
    done = 0
    for page in pages:
        result = await analyzer.analyze(page, goal)
        done += 1
        if result is None:
            continue
        fields = {k: {"value": v.value, "evidence": v.evidence} for k, v in (result.extracted or {}).items()}
        detail[page.url_key] = {
            "classification": result.classification,
            "relevance": result.feedback.relevance_score,
            "fields": fields,
        }
        kept += len(fields)
        if done % 10 == 0:
            print(f"    {done}/{len(pages)} pages, {budget.used:,} tokens so far", flush=True)

    logging.getLogger("crawlme.analyzer.page_analyzer").removeHandler(counter)
    await analyzer.aclose()
    return {
        "detail": detail,
        "verdicts": {k: v["classification"] for k, v in detail.items()},
        "kept": kept,
        "rejected": counter.rejected,
        "out": budget.output_tokens,
        "thinking": budget.reasoning_tokens,
    }


def _excerpt(page: Page, needle: str, width: int = 160) -> str:
    """The page text around a quote, so a claim can be judged in place."""
    text = " ".join((page.plain_text or "").split())
    i = text.find(needle[:40]) if needle else -1
    if i < 0:
        return text[:width] + ("..." if len(text) > width else "")
    start = max(0, i - width // 3)
    return ("..." if start else "") + text[start : start + width] + "..."


def _review(on: dict, off: dict, pages: dict[str, Page]) -> str:
    """The differences a person has to settle, and nothing else.

    Counts said thinking-off keeps more fields.  Counts cannot say
    whether the extra ones are right: the evidence check only proves the
    quote is on the page, not that it answers the field that claimed it.
    """
    d_on, d_off = on["detail"], off["detail"]
    lines = [
        "# What the thinking changed",
        "",
        "Only the differences are here.  For each one the question is the",
        "same: is the value the right answer to the field that claims it?",
        "The quote beside it is already known to be on the page -- that is",
        "what the analyzer checks -- so the thing to judge is the mapping,",
        "not the quotation.",
        "",
    ]
    extra: list[str] = []
    for key in sorted(set(d_on) | set(d_off)):
        page = pages.get(key)
        url = page.url.canonical if page else key
        fon = d_on.get(key, {}).get("fields", {})
        foff = d_off.get(key, {}).get("fields", {})
        for name in sorted(set(fon) | set(foff)):
            a, b = fon.get(name), foff.get(name)
            if a == b:
                continue
            who = "off only" if a is None else ("on only" if b is None else "different value")
            got = b or a
            extra.append(
                f"### {name} ({who})\n\n"
                f"- page: {url}\n"
                + (f"- thinking on:  {a['value']!r}\n" if a else "- thinking on:  (nothing)\n")
                + (f"- thinking off: {b['value']!r}\n" if b else "- thinking off: (nothing)\n")
                + f"- quote: {got['evidence']!r}\n"
                + (f"- in the page: {_excerpt(page, got['evidence'])}\n" if page else "")
            )
    lines += [f"## Fields the two disagree on ({len(extra)})", "", *extra]

    dis = [k for k in set(d_on) & set(d_off) if d_on[k]["classification"] != d_off[k]["classification"]]
    lines += ["", f"## Verdicts the two disagree on ({len(dis)})", ""]
    for key in sorted(dis):
        page = pages.get(key)
        lines.append(
            f"- {page.url.canonical if page else key}\n"
            f"  - thinking on:  {d_on[key]['classification']} ({d_on[key]['relevance']:.2f})\n"
            f"  - thinking off: {d_off[key]['classification']} ({d_off[key]['relevance']:.2f})\n"
            + (f"  - page: {_excerpt(page, '')}\n" if page else "")
        )
    return "\n".join(lines) + "\n"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run", type=Path, help="A finished run directory under results/")
    ap.add_argument("--limit", type=int, default=60, help="Pages to re-analyse (each is analysed twice)")
    ap.add_argument("--off", default="none", help="The value that disables thinking on this provider")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("reasoning-review.md"),
        help="Where to write the differences worth a human eye (and the raw results beside it)",
    )
    args = ap.parse_args()

    goal, pages = load(args.run, args.limit)
    if not pages:
        print("no page has stored text to re-analyse", file=sys.stderr)
        return 1
    spec = (goal.extraction_spec or {}).get("fields", {})
    print(f"{len(pages)} pages, {len(spec)} declared fields: {', '.join(spec) or '(none)'}")
    print(f"goal: {(goal.goal_statement or goal.prompt)[:70]}\n")

    runs = {}
    for name, effort in (("thinking on", ""), (f"thinking off ({args.off})", args.off)):
        print(f"  {name}")
        runs[name] = await analyse_all(goal, pages, effort)
        r = runs[name]
        print(
            f"    -> {r['kept']} fields kept, {r['rejected']} evidence thrown, "
            f"{r['out']:,} output ({r['thinking']:,} thinking)\n",
            flush=True,
        )

    (_, on), (_, off) = runs.items()
    both = set(on["verdicts"]) & set(off["verdicts"])  # type: ignore[arg-type]
    same = sum(1 for k in both if on["verdicts"][k] == off["verdicts"][k])  # type: ignore[index]
    print(f"{'':28}{'fields':>8}{'thrown':>8}{'output':>10}{'thinking':>10}")
    for name, r in runs.items():
        print(f"  {name:26}{r['kept']:>8}{r['rejected']:>8}{r['out']:>10,}{r['thinking']:>10,}")
    if both:
        print(f"\n  the two agreed on {same}/{len(both)} verdicts ({same / len(both):.0%})")

    pages_by_key = {p.url_key: p for p in pages}
    args.out.write_text(_review(on, off, pages_by_key), encoding="utf-8")
    raw = args.out.with_suffix(".json")
    raw.write_text(json.dumps({"on": on["detail"], "off": off["detail"]}, ensure_ascii=False, indent=1), "utf-8")
    print(f"\n  wrote {args.out} (and {raw.name}, so this never has to be paid for twice)")
    await close_litellm_clients()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
