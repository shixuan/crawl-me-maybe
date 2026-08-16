"""Replay: re-run the analysis stage over a completed task's pages.

The pages table is the frozen corpus of a run; replay produces new
judgments over it without touching anything else.  It never writes to
any table but analyses (plus one new crawl_goals row when a fresh
prompt is given), and it never goes through the steering subsystem
(the analyzer is called directly): a replay prompt has not been
validated by a live crawl, so its signals must not pollute
results/feedback.db.

Idempotency.  A page's analysis identity is (url_key, goal_id,
prompt_version, model).  Before each analyze, replay checks whether
that identity already exists and skips it, so replay-of-replay is a
no-op.  Goal ids are content-derived (sha256 of the prompt), so
replaying the same prompt twice is a no-op too.  --force skips the
check and appends new rows for variance studies (old rows are never
touched; the identity columns simply repeat).
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from crawlme.analyzer.page_analyzer import _PROMPT_VERSION, Analyzer, PageAnalyzer
from crawlme.config import Settings
from crawlme.llm import TokenBudget, TokenBudgetError, close_litellm_clients
from crawlme.logging import setup_logging
from crawlme.pioneer.goal_enhancer import GoalEnhancer
from crawlme.schemas import URL, AnalysisResult, CrawlGoal, Page
from crawlme.storage.sqlite.crawl_db import SqliteCrawlDb

logger = logging.getLogger(__name__)


class ReplayError(Exception):
    """Replay cannot proceed: task not found, no credentials, etc."""


async def cmd_replay(args: argparse.Namespace) -> None:
    """The ``crawl replay`` command: settings layering, run, report."""
    cfg = Settings()
    # Flags override env/defaults (same layering as run, see config.py).
    if args.analyzer_max_chars is not None:
        cfg.analyzer_max_chars = args.analyzer_max_chars
    if args.log_level is not None:
        cfg.log_level = args.log_level
    # Replay is the first (and only) logger configuration on this path.
    setup_logging(cfg)
    try:
        report = await run_replay(
            cfg,
            args.task_id,
            prompt=args.prompt,
            limit=args.limit,
            max_tokens=args.max_tokens,
            force=args.force,
        )
    except ReplayError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    # Same teardown as run: close litellm's cached async clients while
    # the loop is alive, so its shutdown noise never prints after the
    # report.
    await close_litellm_clients()
    print_replay_summary(report)
    logging.getLogger().setLevel(logging.CRITICAL)


@dataclass
class ReplayReport:
    """The numbers a replay run ends with (rendered by the CLI)."""

    task_id: str
    run_dir: Path
    state: str
    goal_id: str
    new_goal: bool
    pages_total: int
    analyzed: int
    retried_ok: int
    skipped: int
    empty: int
    failed: int
    published: int
    tokens_in: int
    tokens_out: int
    tokens_used: int
    llm_calls: int


async def run_replay(
    settings: Settings,
    task_id: str,
    *,
    prompt: str | None = None,
    limit: int | None = None,
    max_tokens: int | None = None,
    force: bool = False,
    analyzer: Analyzer | None = None,
) -> ReplayReport:
    """Re-analyze a completed task's pages.

    *analyzer* is injectable for tests; None (the default) builds one
    from settings exactly like a live run does.  Pages are processed
    in fetch order, one at a time, and parked failures are waited out
    through drain_pending() so nothing is lost when the analyzer
    closes.
    """
    run_dir, task_row = await find_run_dir(settings.result_dir, task_id)
    budget = TokenBudget(limit=max_tokens or 0)
    if analyzer is None:
        analyzer = PageAnalyzer.from_settings(settings, budget=budget)
        if analyzer is None:
            raise ReplayError("replay needs LLM credentials to analyze pages: configure LLM_API_KEY / LLM_BASE_URL")

    storage = SqliteCrawlDb(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
    await storage.start()
    try:
        goal_row = await storage.get_goal(task_row["goal_id"])
        if goal_row is None:
            raise ReplayError(f"goal {task_row['goal_id']} missing from the run database")
        goal = _goal_from_row(goal_row)
        new_goal = False
        if prompt:
            # A replay prompt names one judging context: CrawlGoal ids
            # are content-derived (sha256 of the prompt), so the same
            # text maps to the same goal and replaying a prompt twice
            # is idempotent like the no-prompt case.
            goal = CrawlGoal(prompt=prompt)
            existing = await storage.get_goal(goal.goal_id)
            if existing is None:
                new_goal = True
                enhanced = await GoalEnhancer.from_settings(settings, budget=budget).enhance(goal)
                if enhanced is not None:
                    goal.goal_statement = enhanced.statement
                    goal.keywords = enhanced.keywords
                    goal.since = enhanced.since
                storage.save_goal(goal.model_dump(mode="json"))
            else:
                # Same prompt replayed before: reuse the stored goal
                # (already enhanced), never pay the enhancer again.
                goal = _goal_from_row(existing)

        published = 0

        def _sink(result: AnalysisResult) -> None:
            # First tries and background retries land here alike.
            nonlocal published
            published += 1
            storage.save_analysis(result.model_dump(mode="json"))

        analyzer.bind_sink(_sink)

        rows = await storage.list_pages()
        pages_total = len(rows)
        if limit is not None:
            rows = rows[:limit]
        analyzed = parked = skipped = empty = 0
        try:
            for row in rows:
                page = _page_from_row(row)
                # Mirrors the analyzer's own empty-text skip, so the
                # report can tell "no text" apart from "parked".
                if not ((page.plain_text or "").strip() or (page.markdown or "").strip()):
                    empty += 1
                    continue
                if not force and await storage.has_analysis(
                    page.url_key, goal.goal_id, _PROMPT_VERSION, settings.llm_model
                ):
                    skipped += 1
                    continue
                budget.check()
                result = await analyzer.analyze(page, goal)
                if result is not None:
                    analyzed += 1
                else:
                    parked += 1
        except TokenBudgetError:
            logger.warning("replay.budget_exhausted used=%d/%d", budget.used, budget.limit)

        # Parked pages retry in the background; wait them out so every
        # one settles before the analyzer (and this run) closes.
        await analyzer.drain_pending()
        retried_ok = published - analyzed
        failed = parked - retried_ok

        return ReplayReport(
            task_id=task_id,
            run_dir=run_dir,
            state=task_row.get("state", ""),
            goal_id=goal.goal_id,
            new_goal=new_goal,
            pages_total=pages_total,
            analyzed=analyzed,
            retried_ok=retried_ok,
            skipped=skipped,
            empty=empty,
            failed=failed,
            published=published,
            tokens_in=budget.input_tokens,
            tokens_out=budget.output_tokens,
            tokens_used=budget.used,
            llm_calls=budget.calls,
        )
    finally:
        await analyzer.aclose()
        await storage.close()


async def find_run_dir(result_dir: Path, task_id: str) -> tuple[Path, dict[str, Any]]:
    """Locate the run directory holding *task_id* under results/.

    Run dirs are results/<timestamp>/ with no task index, so this
    scans every candidate db/crawl.db (newest first) until the task
    row turns up.  Returns (run_dir, task_row).  Raises ReplayError
    when nothing holds the task, listing what was found.
    """
    seen: dict[str, str] = {}
    for db_path in sorted(result_dir.glob("*/db/crawl.db"), reverse=True):
        try:
            async with aiosqlite.connect(db_path) as conn:
                conn.row_factory = aiosqlite.Row
                cur = await conn.execute("SELECT * FROM crawl_tasks WHERE task_id = ?", (task_id,))
                row = await cur.fetchone()
                if row is not None:
                    return db_path.parent.parent, dict(row)
                cur = await conn.execute("SELECT task_id FROM crawl_tasks")
                for r in await cur.fetchall():
                    seen[r["task_id"]] = db_path.parent.parent.name
        except sqlite3.Error:
            # A stray or unreadable database; keep scanning.
            continue
    if seen:
        detail = ", ".join(f"{tid} ({ts})" for tid, ts in sorted(seen.items()))
        raise ReplayError(f"task {task_id} not found; runs under {result_dir} hold: {detail}")
    raise ReplayError(f"task {task_id} not found: no run databases under {result_dir}")


def print_replay_summary(r: ReplayReport) -> None:
    """Render the replay report as aligned terminal lines."""
    goal = r.goal_id
    if r.new_goal:
        goal += " (new prompt)"
    lines = [f"replay finished: {r.task_id}"]
    lines.append(f"  run:        {r.run_dir} (state={r.state})")
    lines.append(f"  goal:       {goal}")
    lines.append(f"  pages:      {r.pages_total} in run")
    parts = [f"{r.analyzed} first-try"]
    if r.retried_ok:
        parts.append(f"{r.retried_ok} retried")
    lines.append(f"  analyses:   {r.published} written ({', '.join(parts)})")
    if r.skipped or r.empty:
        skip_parts = []
        if r.skipped:
            skip_parts.append(f"{r.skipped} identical")
        if r.empty:
            skip_parts.append(f"{r.empty} empty text")
        lines.append(f"  skipped:    {', '.join(skip_parts)}")
    if r.failed:
        lines.append(f"  failed:     {r.failed}")
    tokens = f"{r.tokens_used}"
    if r.llm_calls:
        tokens += f" ({r.tokens_in} in / {r.tokens_out} out), {r.llm_calls} calls"
    lines.append(f"  tokens:     {tokens}")
    print("\n".join(lines))


def _goal_from_row(row: dict[str, Any]) -> CrawlGoal:
    """Rebuild a CrawlGoal from a stored row.

    save_goal stores the JSON-shaped fields (keywords, embedding,
    extraction_spec) as JSON text columns, so they must be decoded
    before validation.
    """
    data = dict(row)
    data["keywords"] = json.loads(data.get("keywords") or "[]")
    data["embedding"] = json.loads(data["embedding"]) if data.get("embedding") else None
    data["extraction_spec"] = json.loads(data["extraction_spec"]) if data.get("extraction_spec") else None
    return CrawlGoal.model_validate(data)


def _page_from_row(row: dict[str, Any]) -> Page:
    """Rebuild a Page from a stored row (the frozen corpus)."""
    return Page(
        page_id=row["page_id"],
        url_key=row["url_key"],
        url=URL.model_validate(json.loads(row["url_json"])),
        raw_html_path=row.get("raw_html_path", ""),
        title=row.get("title"),
        markdown=row.get("markdown"),
        plain_text=row.get("plain_text"),
        metadata=json.loads(row.get("metadata_json") or "{}"),
        text_hash=row.get("text_hash", ""),
        text_len=row.get("text_len", 0),
        extracted_at=_parse_ts(row.get("extracted_at")),
        extraction_status=row.get("extraction_status", "OK"),
    )


def _parse_ts(value: Any) -> datetime.datetime:
    if isinstance(value, str) and value:
        return datetime.datetime.fromisoformat(value)
    return datetime.datetime.now(datetime.timezone.utc)
