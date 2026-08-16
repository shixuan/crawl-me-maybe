"""The inspect command: a read-only look at a task's results.

No LLM, no writes: opens the run's database and renders what the task
produced — goals, pages, analyses by classification, and the top
relevant pages.  --export dumps the pages-and-analyses join (the
product users consume) as json or csv.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crawlme.cli.replay import ReplayError, find_run_dir
from crawlme.config import Settings
from crawlme.storage.sqlite.crawl_db import SqliteCrawlDb


class InspectError(Exception):
    """Inspect cannot proceed: unknown goal, etc."""


@dataclass
class InspectData:
    """Everything the inspect command renders or exports."""

    task_id: str
    run_dir: Path
    state: str
    reason: str
    goals: list[dict[str, Any]]
    task_goal_id: str
    goal_id: str
    pages: list[dict[str, Any]]
    analyses: list[dict[str, Any]]
    goal_counts: dict[str, int]


async def inspect_task(settings: Settings, task_id: str, *, goal_id: str | None = None) -> InspectData:
    """Read one task's results out of its run database.

    *goal_id* selects whose analyses to look at; None (the default)
    means the task's original goal.  Replay goals are visible through
    the returned goal rows and counts, so callers can list them.
    """
    run_dir, task_row = await find_run_dir(settings.result_dir, task_id)
    storage = SqliteCrawlDb(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
    await storage.start()
    try:
        goals = await storage.list_goals()
        goal_ids = [g["goal_id"] for g in goals]
        if goal_id is None:
            goal_id = task_row.get("goal_id", "")
        if goal_id not in goal_ids:
            raise InspectError(f"goal {goal_id} not found in the run database")

        all_analyses = await storage.list_analyses()
        goal_counts: dict[str, int] = {}
        for a in all_analyses:
            g = a.get("goal_id", "")
            goal_counts[g] = goal_counts.get(g, 0) + 1
        analyses = [a for a in all_analyses if a.get("goal_id") == goal_id]

        return InspectData(
            task_id=task_id,
            run_dir=run_dir,
            state=task_row.get("state", ""),
            reason=task_row.get("stopping_reason") or "",
            goals=goals,
            task_goal_id=task_row.get("goal_id", ""),
            goal_id=goal_id,
            pages=await storage.list_pages(),
            analyses=analyses,
            goal_counts=goal_counts,
        )
    finally:
        await storage.close()


async def cmd_inspect(args: argparse.Namespace) -> None:
    """The ``crawl inspect`` command: read-only results view."""
    try:
        data = await inspect_task(Settings(), args.task_id, goal_id=args.goal)
    except (ReplayError, InspectError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if args.export:
        _export(data, args.export)
    else:
        _print_summary(data)


def _print_summary(data: InspectData) -> None:
    """Render the inspect summary as aligned terminal lines."""
    goal = next((g for g in data.goals if g["goal_id"] == data.goal_id), None)
    by_class = Counter(a.get("classification", "UNKNOWN") for a in data.analyses)
    pages_by_key = {p["url_key"]: p for p in data.pages}

    lines = [
        f"task:      {data.task_id} (state={data.state}, reason={data.reason or 'none'})",
        f"run:       {data.run_dir}",
        f"pages:     {len(data.pages)} fetched",
    ]
    if goal is not None:
        role = "original" if data.goal_id == data.task_goal_id else "replay"
        lines.append(f'goal:      {data.goal_id} "{goal.get("prompt", "")[:60]}" ({role})')

    analyses_line = f"analyses:  {len(data.analyses)}"
    if by_class:
        parts = ", ".join(f"{n} {c}" for c, n in sorted(by_class.items(), key=lambda kv: -kv[1]))
        analyses_line += f" ({parts})"
        models = sorted({a["model"] for a in data.analyses if a.get("model")})
        if models:
            analyses_line += f" [model: {', '.join(models)}]"
    lines.append(analyses_line)

    others = [g for g in data.goals if g["goal_id"] != data.goal_id]
    if others:
        parts = ", ".join(
            f'{g["goal_id"]} "{g.get("prompt", "")[:40]}" '
            f"({data.goal_counts.get(g['goal_id'], 0)}, "
            f"{'original' if g['goal_id'] == data.task_goal_id else 'replay'})"
            for g in others
        )
        lines.append(f"other goals: {parts}")

    relevant = [a for a in data.analyses if a.get("classification") == "RELEVANT"]
    # One line per page: replays may have judged the same page several
    # times, but the summary lists pages, not rows.
    best_by_key: dict[str, dict[str, Any]] = {}
    for a in relevant or data.analyses:
        key = a.get("url_key", "")
        if key not in best_by_key or a.get("relevance_score", 0.0) > best_by_key[key].get("relevance_score", 0.0):
            best_by_key[key] = a
    top = sorted(best_by_key.values(), key=lambda a: -a.get("relevance_score", 0.0))[:10]
    if top:
        lines.append("top relevant:")
        for a in top:
            page = pages_by_key.get(a.get("url_key"))
            url = json.loads(page["url_json"]).get("canonical", "") if page else ""
            title = (page.get("title") or "") if page else ""
            lines.append(f"  {a.get('relevance_score', 0.0):.2f}  {title} — {url}")
            summary = (a.get("summary") or "").strip()
            if summary:
                lines.append(f"    {summary[:100]}")
    print("\n".join(lines))


def _export(data: InspectData, fmt: str) -> None:
    """Dump the pages-and-analyses join to stdout."""
    pages_by_key = {p["url_key"]: p for p in data.pages}
    rows: list[dict[str, Any]] = []
    for a in data.analyses:
        page = pages_by_key.get(a.get("url_key"))
        feedback = json.loads(a.get("feedback_json") or "{}")
        rows.append(
            {
                "url": json.loads(page["url_json"]).get("canonical", "") if page else "",
                "title": (page.get("title") or "") if page else "",
                "goal_id": a.get("goal_id", ""),
                "classification": a.get("classification", "UNKNOWN"),
                "relevance_score": a.get("relevance_score", 0.0),
                "hub_score": feedback.get("hub_score", 0.0),
                "summary": a.get("summary") or "",
                "tags": json.loads(a.get("tags_json") or "[]"),
                "model": a.get("model", ""),
                "prompt_version": a.get("prompt_version", ""),
                "analyzed_at": a.get("analyzed_at", ""),
            }
        )
    if fmt == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    fieldnames = [
        "url",
        "title",
        "goal_id",
        "classification",
        "relevance_score",
        "hub_score",
        "summary",
        "tags",
        "model",
        "prompt_version",
        "analyzed_at",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        row["tags"] = ",".join(row["tags"])
        writer.writerow(row)
