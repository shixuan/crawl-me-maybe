"""Feedback subsystem benchmark: does the analyzer earn its tokens?

Every arm runs the same goal, seeds, and page budget, differing only
in the feedback configuration:

  A            --feedback off            clean baseline: no analyzer,
                                         no signals, no prior load
  C<cap>       --analyzer-max-chars <cap> one arm per text cap, e.g.
                                         C3000 / C4500 / C6000 / C12000

Then one shared LLM judge labels every unique fetched page with the
same classification schema but a 12k-char text cap, so its verdict has
more information than any steering decision.  The primary metric is
the judge-relevant page count at each budget checkpoint, reported per
arm with token cost and wall time.

Everything is scoped per goal and per run: every invocation
creates a fresh timestamped run set under
results/bench/<goal-hash>/<timestamp>/<arm>/, so ten runs make ten
independent data points.  Judge labels live in
data/judge_<goal-hash>.json shared across the runs (URLs judged once,
new pages judged incrementally).  An interrupted run set resumes with
--resume <run_id>.

Usage:
  python3 benchmark/feedback/feedback_bench.py \
      --goal "..." --seeds "https://a,https://b" --max-pages 100 \
      --caps 3000,4500,6000,12000
  python3 benchmark/feedback/feedback_bench.py --resume 20260815_093000
  python3 benchmark/feedback/feedback_bench.py --report-only
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

from crawlme.config import Settings
from crawlme.feedback.analyzer import _SYSTEM as JUDGE_SYSTEM
from crawlme.llm import LLMClient, litellm_loaded, parse_json_response

REPO = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"

#: The judge sees twice the default analyzer cap: information advantage
#: is what makes its verdict the reference the arms are measured by.
JUDGE_MAX_CHARS = 12_000
JUDGE_CONCURRENCY = 4
_RELEVANCE_FLOOR = 0.7

# Fullwidth commas are part of the Chinese prompt text, not code
# punctuation, so they stay as-is (noqa on each line below).
_DEFAULT_GOAL = (
    "调查 2026 年 AI 编程助手（coding agent）在生产环境中的真实落地情况："  # noqa: RUF001
    "团队如何评估与选型（基准测试、成本、安全合规），"  # noqa: RUF001
    "常见失败模式与工程团队的反面经验，IDE 插件 vs CLI 工具的实际体验对比。"  # noqa: RUF001
    "优先一线工程师的博客和长文复盘，中英文不限"  # noqa: RUF001
)
_DEFAULT_SEEDS = (
    "https://news.ycombinator.com,https://lobste.rs,https://github.com,"
    "https://huggingface.co/blog,https://simonwillison.net"
)
_DEFAULT_CAPS = "3000,4500,6000,12000"
_CHECKPOINTS = (5, 10, 20, 40, 60, 80, 100)


def build_arms(caps: list[int]) -> dict[str, tuple[str, list[str]]]:
    """The arm table: the feedback-off baseline plus one arm per cap."""
    arms = {"off": ("off (feedback off)", ["--feedback", "off"])}
    for cap in caps:
        arms[f"C{cap}"] = (f"C{cap} (chars {cap})", ["--analyzer-max-chars", str(cap)])
    return arms


def goal_hash(goal: str) -> str:
    """A short stable identifier for the goal's run dirs and judge file."""
    return hashlib.sha256(goal.encode()).hexdigest()[:8]


#: run arms -----------------------------------------------------------


def latest_run_dir(out_dir: Path) -> Path | None:
    """The newest timestamped run dir inside an arm dir.

    SqliteCrawlDb.create(result_dir) nests every run under a
    results/<arm>/<timestamp>/ directory, so the arm's real data is
    one level down.  Timestamps sort lexicographically.
    """
    subs = [p for p in out_dir.glob("*") if p.is_dir() and p.name[:8].isdigit()]
    return max(subs, key=lambda p: p.name) if subs else None


def run_completed(out_dir: Path) -> bool:
    # "crawl finished" is stdout-only, so the file log is checked for
    # the CLI's final logger line instead.  A completed run dir is
    # immutable: replicating an arm means moving the dir aside first,
    # never re-running into the same feedback.db.
    run = latest_run_dir(out_dir)
    log = run / "log" if run else None
    return log is not None and log.exists() and "state=COMPLETED" in log.read_text(errors="replace")


def run_arms(arms: dict[str, tuple[str, list[str]]], goal: str, seeds: str, max_pages: int, bench_dir: Path) -> None:
    for key, (label, extra) in arms.items():
        out_dir = bench_dir / key.lower()
        if run_completed(out_dir):
            print(f"[skip] arm {label}: {out_dir} already crawled")
            continue
        print(f"[run] arm {label} (its output streams below)")
        started = time.monotonic()
        argv = [
            sys.executable,
            "-c",
            "from crawlme.cli import main; main()",
            "run",
            goal,
            "--seeds",
            seeds,
            "--max-pages",
            str(max_pages),
            "--result-dir",
            str(out_dir),
            "--ignore-robots",
            "--log-level",
            "DEBUG",
            *extra,
        ]
        # All argv parts come from this script's fixed table plus
        # CLI-provided goal/seeds strings.  Output is inherited, not
        # captured: an arm runs for minutes and its progress should
        # stream to the terminal instead of hiding behind a pipe.
        result = subprocess.run(argv, cwd=REPO)  # noqa: S603
        if result.returncode != 0:
            print(f"[fail] arm {label} exit={result.returncode}")
            sys.exit(1)
        print(f"[done] arm {label} in {time.monotonic() - started:.0f}s")


#: collect ------------------------------------------------------------


def collect_pages(out_dir: Path) -> list[dict]:
    """Fetched pages in fetch order, oldest first."""
    run = latest_run_dir(out_dir)
    db = run / "db" / "crawl.db" if run else None
    if db is None or not db.exists():
        return []
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT url_json, title, plain_text FROM pages ORDER BY extracted_at").fetchall()
    conn.close()
    pages = []
    for url_json, title, text in rows:
        url = json.loads(url_json)
        pages.append({"url": url.get("canonical", ""), "title": title, "text": text or ""})
    return pages


#: judge --------------------------------------------------------------


def _judge_prompt(goal_text: str, page: dict) -> str:
    lines = ["## Goal", goal_text, "## Page", page["url"]]
    if page["title"]:
        lines.append(f"Title: {page['title']}")
    lines.append("")
    lines.append(page["text"][:JUDGE_MAX_CHARS])
    return "\n".join(lines)


async def judge_pages(goal: str, pages: list[dict], judge_path: Path) -> None:
    """Label every unique page once; persist incrementally."""
    judge_path.parent.mkdir(parents=True, exist_ok=True)
    if judge_path.exists():
        judged = json.loads(judge_path.read_text()).get("pages", {})
    else:
        judged = {}
    todo = [p for p in pages if p["url"] and p["url"] not in judged and p["text"]]
    if not todo:
        print(f"[judge] all {len(judged)} pages already labeled")
        return
    print(f"[judge] {len(todo)} pages to label")

    # Settings kwargs override .env: credentials still load from it,
    # only the judge's concurrency is pinned.
    client = LLMClient.from_settings(Settings(llm_concurrency=JUDGE_CONCURRENCY))

    async def one(page: dict) -> tuple[str, dict]:
        resp = await client.chat(
            _judge_prompt(goal, page),
            system=JUDGE_SYSTEM,
            max_tokens=1024,
            json_mode=True,
        )
        data = parse_json_response(resp.content) or {}
        classification = str(data.get("classification", "UNKNOWN")).upper()
        relevance = float(data.get("relevance_score") or 0.0)
        return page["url"], {
            "title": page["title"],
            "classification": classification,
            "relevance": round(relevance, 3),
            "tokens": resp.input_tokens + resp.output_tokens,
        }

    for batch_start in range(0, len(todo), 10):
        batch = todo[batch_start : batch_start + 10]
        results = await asyncio.gather(*(one(p) for p in batch))
        for url, label in results:
            judged[url] = label
        # Persist every batch: a mid-run crash keeps its progress.
        judge_path.write_text(json.dumps({"goal": goal, "pages": judged}, ensure_ascii=False, indent=2))
        print(f"[judge] labeled {len(judged)} pages so far")


def judged_relevant(url: str, judge: dict) -> bool:
    label = judge.get("pages", {}).get(url, {})
    return label.get("classification") == "RELEVANT" and label.get("relevance", 0.0) >= _RELEVANCE_FLOOR


#: report -------------------------------------------------------------


def arm_stats(out_dir: Path) -> dict:
    """Pages, tokens, wall time, and analyses from the run's log."""
    pages = collect_pages(out_dir)
    stats = {"pages": len(pages), "pages_list": pages, "tokens": 0, "time": 0.0, "analyses": 0}
    run = latest_run_dir(out_dir)
    log = run / "log" if run else None
    if log is None or not log.exists():
        return stats
    for line in log.read_text(errors="replace").splitlines():
        if "task.done" in line and "tokens=" in line:
            try:
                stats["tokens"] = int(line.split("tokens=")[1].split()[0])
            except (ValueError, IndexError):
                pass
        if "fetch.ok" in line and "elapsed=" in line:
            try:
                stats["time"] = max(stats["time"], float(line.split("elapsed=")[1].split("s")[0]))
            except (ValueError, IndexError):
                pass
        if "analysis.ok" in line:
            stats["analyses"] += 1
    return stats


def run_sets(bench_root: Path) -> list[Path]:
    """All run sets under the goal dir, oldest first.

    A run set is one full experiment (all arms once), named by the
    timestamp it was started.  Old flat arm dirs (pre-run-set layout)
    have non-timestamp names and are ignored.
    """
    subs = [p for p in bench_root.glob("*") if p.is_dir() and p.name[:8].isdigit()]
    return sorted(subs, key=lambda p: p.name)


def report(
    goal: str,
    max_pages: int,
    arms: dict[str, tuple[str, list[str]]],
    bench_root: Path,
    judge_path: Path,
) -> None:
    if not judge_path.exists():
        print("no judge data yet: run the judge phase first")
        sys.exit(1)
    judge = json.loads(judge_path.read_text())
    run_dirs = run_sets(bench_root)
    if not run_dirs:
        print("no run sets yet: run the arms first")
        return

    # Per-run history: every run is one data point, and single runs
    # are noisy, so the aggregate rows below are the real verdict.
    print(f"\nfeedback benchmark: {goal[:60]}...")
    print(f"\njudge hits @{max_pages} per run:")
    header = f"{'run':<17}" + "".join(f"{key:<9}" for key in arms)
    print(header)
    per_arm: dict[str, list[int]] = {k: [] for k in arms}
    for rd in run_dirs:
        row = [arm_hits(rd / k.lower(), judge, max_pages) for k in arms]
        for k, v in zip(arms, row):
            per_arm[k].append(v)
        print(f"{rd.name:<17}" + "".join(f"{v:<9}" for v in row))

    med = {k: statistics.median(v) for k, v in per_arm.items()}
    lo = {k: min(v) for k, v in per_arm.items()}
    hi = {k: max(v) for k, v in per_arm.items()}
    print(f"{'median':<17}" + "".join(f"{med[k]:<9.1f}" for k in arms))
    print(f"{'range':<17}" + "".join(f"{lo[k]}-{hi[k]:<7}" for k in arms))
    tok_med = {k: int(statistics.median(arm_stats(rd / k.lower())["tokens"] for rd in run_dirs)) for k in arms}
    print(f"{'tokens(med)':<17}" + "".join(f"{tok_med[k]:<9}" for k in arms))

    print("\nmedian vs off (feedback off):")
    for key in list(arms)[1:]:
        delta = med[key] - med["off"]
        print(f"  {key}: {delta:+.1f}")

    # Latest run set detail: the full checkpoint curve.
    latest = run_dirs[-1]
    checkpoints = [k for k in _CHECKPOINTS if k <= max_pages]
    print(f"\nlatest run {latest.name} detail:")
    hit_cols = "  ".join(f"@{k:<4}" for k in checkpoints)
    header = f"arm                  pages  {hit_cols}  tokens    time    analyses"
    print(header)
    print("-" * len(header))
    for key, (label, _) in arms.items():
        s = arm_stats(latest / key.lower())
        fetched = s["pages_list"]
        hits = [sum(1 for p in fetched[:k] if judged_relevant(p["url"], judge)) for k in checkpoints]
        hit_str = "  ".join(f"{h:<4}" for h in hits)
        print(f"{label:<24} {len(fetched):<6} {hit_str}  {s['tokens']:<9} {s['time']:.0f}s     {s['analyses']}")


def arm_hits(arm_dir: Path, judge: dict, k: int) -> int:
    """Judge-relevant count among the first k fetched pages of an arm."""
    return sum(1 for p in collect_pages(arm_dir)[:k] if judged_relevant(p["url"], judge))


#: main ---------------------------------------------------------------


def new_run_id(bench_root: Path) -> str:
    """A fresh run-set name; suffixes _2, _3 on same-second collisions."""
    base = time.strftime("%Y%m%d_%H%M%S")
    run_id, n = base, 2
    while (bench_root / run_id).exists():
        run_id = f"{base}_{n}"
        n += 1
    return run_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Feedback subsystem benchmark")
    parser.add_argument("--goal", default=_DEFAULT_GOAL, help="Crawl goal (shared by all arms and the judge)")
    parser.add_argument("--seeds", default=_DEFAULT_SEEDS, help="Comma-separated seed URLs")
    parser.add_argument("--max-pages", type=int, default=100, help="Page budget per arm")
    parser.add_argument(
        "--caps",
        default=_DEFAULT_CAPS,
        help="Comma-separated analyzer text caps, one arm per cap (e.g. 3000,4500,6000,12000)",
    )
    parser.add_argument(
        "--resume",
        default=None,
        help="Reuse an existing run set (its timestamp id), skipping completed arms in it",
    )
    parser.add_argument("--report-only", action="store_true", help="Only print the report from existing data")
    args = parser.parse_args()

    caps = [int(c.strip()) for c in args.caps.split(",") if c.strip()]
    arms = build_arms(caps)
    bench_root = REPO / "results" / "bench" / goal_hash(args.goal)
    judge_path = DATA_DIR / f"judge_{goal_hash(args.goal)}.json"

    if args.resume:
        run_dir = bench_root / args.resume
        if not run_dir.exists():
            print(f"[fail] run set {args.resume} not found under {bench_root}")
            sys.exit(1)
    else:
        run_dir = bench_root / new_run_id(bench_root)

    if not args.report_only:
        run_arms(arms, args.goal, args.seeds, args.max_pages, run_dir)

    # Judge the union across every run set, not just the latest one:
    # labels are shared per goal, so later runs only pay for new URLs.
    pages: list[dict] = []
    seen: set[str] = set()
    for rd in run_sets(bench_root):
        for key in arms:
            for p in arm_stats(rd / key.lower())["pages_list"]:
                if p["url"] and p["url"] not in seen:
                    seen.add(p["url"])
                    pages.append(p)

    asyncio.run(_judge_then_cleanup(args.goal, pages, judge_path))
    # Interpreter teardown still fires litellm's atexit worker and
    # asyncio's loop-close debug records; mute them like the CLI does
    # after its final report.
    logging.getLogger().setLevel(logging.CRITICAL)
    report(args.goal, args.max_pages, arms, bench_root, judge_path)


async def _judge_then_cleanup(goal: str, pages: list[dict], judge_path: Path) -> None:
    try:
        await judge_pages(goal, pages, judge_path)
    finally:
        # litellm caches async HTTP clients that only tear down when
        # the loop closes, which then logs scary SSL write errors.
        # Close them while the loop is still alive, the same ritual
        # the CLI's run path performs.
        if litellm_loaded():
            try:
                from litellm.llms.custom_httpx.async_client_cleanup import close_litellm_async_clients

                await close_litellm_async_clients()
            except Exception as e:
                logging.getLogger(__name__).debug("judge.shutdown cleanup best-effort failed: %s", e)
            await asyncio.sleep(0.2)


if __name__ == "__main__":
    main()
