"""The inspect command: a read-only look at a task's results.

No LLM, no writes: opens the run's database and renders what the task
produced — goals, pages, analyses by classification, and the top
relevant pages.  --export dumps the pages-and-analyses join (the
product users consume) as json or csv.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crawlme.cli.cutoff import read_cutoff
from crawlme.cli.replay import ReplayError, find_run_dir
from crawlme.config import Settings
from crawlme.storage.sqlite.crawl_db import SqliteCrawlDb
from crawlme.util.dates import LATER, OVER, UNDATED, group_of


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
        horizon = read_cutoff(args.during, flag="--during", ahead=True).date() if args.during else None
        _print_summary(data, horizon=horizon)


def _print_summary(data: InspectData, *, horizon: datetime.date | None = None) -> None:
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
    lines.extend(_result_lines(best_by_key.values(), pages_by_key, horizon=horizon))
    print("\n".join(lines))


def _result_lines(
    analyses: Iterable[dict[str, Any]],
    pages_by_key: dict[str, Any],
    *,
    horizon: datetime.date | None = None,
) -> list[str]:
    """The results, grouped by whether they have run out.

    Sorted by when they end rather than by score, because a reader
    coming to this asks what is still ahead of them. Nothing is hidden:
    a page that named no date is not a page that fails the dates, and
    across seven runs that was half of them.

    *horizon* is how far ahead still counts as open. What starts after
    it is split off rather than dropped, because how far ahead a reader
    cares about is a preference and being wrong about it should cost a
    heading, not a result. Something that only says when it ends is
    already running, so it stays open however far off that end is.
    """
    today = datetime.datetime.now(datetime.timezone.utc).date()
    live: list[tuple[datetime.date | None, dict[str, Any]]] = []
    undated: list[dict[str, Any]] = []
    over: list[dict[str, Any]] = []
    later: list[tuple[datetime.date, dict[str, Any]]] = []
    for a in analyses:
        ends = _as_date(a.get("ends_on"))
        starts = _as_date(a.get("starts_on"))
        group = group_of(starts, ends, today, horizon)
        if group == UNDATED:
            undated.append(a)
        elif group == OVER:
            over.append(a)
        elif group == LATER and starts is not None:
            later.append((starts, a))
        else:
            live.append((ends, a))
    live.sort(key=lambda pair: (pair[0] is None, pair[0] or today))

    out: list[str] = []
    if live:
        out.append(f"still open ({len(live)}):")
        out += [_one_result(a, pages_by_key, ends, today) for ends, a in live[:10]]
    if undated:
        out.append(f"no date given ({len(undated)}):")
        out += [_one_result(a, pages_by_key, None, today) for a in undated[:10]]
    if later:
        later.sort(key=lambda pair: pair[0])
        out.append(f"starts after {horizon:%b %d} ({len(later)}):")
        out += [_one_result(a, pages_by_key, starts, today, ahead=True) for starts, a in later[:10]]
    if over:
        out.append(f"already over ({len(over)}), newest first:")
        over.sort(key=lambda a: _as_date(a.get("ends_on")) or today, reverse=True)
        out += [_one_result(a, pages_by_key, _as_date(a.get("ends_on")), today) for a in over[:5]]
    return out


def _one_result(
    a: dict[str, Any],
    pages_by_key: dict[str, Any],
    when_on: datetime.date | None,
    today: datetime.date,
    *,
    ahead: bool = False,
) -> str:
    """One result line. *ahead* says the date is when it starts, not when it ends."""
    page = pages_by_key.get(str(a.get("url_key") or ""))
    url = json.loads(page["url_json"]).get("canonical", "") if page else ""
    title = (page.get("title") or "") if page else ""
    when = "no date"
    if when_on is not None:
        days = (when_on - today).days
        tail = (
            f", in {days}d"
            if ahead
            else (f", {days}d left" if days > 0 else ", today" if days == 0 else f", {-days}d ago")
        )
        when = f"{when_on:%b %d}{tail}"
    return f"  {when:>18}  {a.get('relevance_score', 0.0):.2f}  {title} — {url}"


def _as_date(raw: Any) -> datetime.date | None:
    """Dates come back from storage as ISO text."""
    if isinstance(raw, datetime.date):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _export(data: InspectData, fmt: str) -> None:
    """Dump the pages-and-analyses join to stdout.

    The json form is the one meant to be read by something other than a
    person: it carries the extracted fields with the page text backing
    each one, so whatever renders it can show a value and let the reader
    check it against the page.  csv stays flat and leaves them out —
    every goal declares its own fields, so there is no stable column set
    to flatten them into, and inventing one per export would make two
    exports of the same run disagree.
    """
    pages_by_key = {p["url_key"]: p for p in data.pages}
    rows: list[dict[str, Any]] = []
    for a in data.analyses:
        page = pages_by_key.get(a.get("url_key"))
        rows.append(
            {
                "url": json.loads(page["url_json"]).get("canonical", "") if page else "",
                "url_key": a.get("url_key", ""),
                "title": (page.get("title") or "") if page else "",
                "published_at": (page.get("published_at") or "") if page else "",
                "goal_id": a.get("goal_id", ""),
                "classification": a.get("classification", "UNKNOWN"),
                "relevance_score": a.get("relevance_score", 0.0),
                "starts_on": a.get("starts_on") or "",
                "ends_on": a.get("ends_on") or "",
                "summary": a.get("summary") or "",
                "tags": json.loads(a.get("tags_json") or "[]"),
                "extracted": json.loads(a.get("extracted_json") or "{}"),
                "model": a.get("model", ""),
                "prompt_version": a.get("prompt_version", ""),
                "spec_version": a.get("spec_version", ""),
                "analyzed_at": a.get("analyzed_at", ""),
            }
        )
    if fmt == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    # csv drops what has no fixed shape; see the docstring.
    for row in rows:
        row.pop("extracted", None)
    fieldnames = [
        "url",
        "url_key",
        "title",
        "published_at",
        "goal_id",
        "classification",
        "relevance_score",
        "starts_on",
        "ends_on",
        "summary",
        "tags",
        "model",
        "prompt_version",
        "spec_version",
        "analyzed_at",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        row["tags"] = ",".join(row["tags"])
        writer.writerow(row)
