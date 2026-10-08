"""Regroup a stored goal's analyses without fetching or analyzing pages."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from crawlme.cli.replay import _goal_from_row, find_run_dir
from crawlme.cli.run import _stage_lines
from crawlme.config import Settings
from crawlme.dedup.grouper import Grouper, group_results
from crawlme.llm import TokenBudget, close_litellm_clients
from crawlme.logging import setup_logging
from crawlme.logging.config import write_report
from crawlme.storage.sqlite import SqliteStorage

logger = logging.getLogger(__name__)


async def cmd_dedup(args: argparse.Namespace) -> None:
    settings = Settings()
    if args.result_dir is not None:
        settings.result_dir = Path(args.result_dir)
    if args.log_level is not None:
        settings.log_level = args.log_level
    setup_logging(settings, force=True)
    budget = TokenBudget(limit=args.max_tokens or 0)
    storage = None
    failed = False
    lines = [f"dedup finished: {args.task_id}"]
    try:
        run_dir, task = await find_run_dir(settings.result_dir, args.task_id)
        if task.get("state") == "RUNNING":
            raise ValueError("task is still marked RUNNING; wait until it finishes before regrouping")
        storage = SqliteStorage(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
        await storage.start()
        goal_id = args.goal or task["goal_id"]
        goal_row = await storage.get_goal(goal_id)
        if goal_row is None:
            raise ValueError(f"goal {goal_id} not found in the run database")
        grouper = Grouper.from_settings(settings, budget=budget)
        if grouper is None:
            raise ValueError("dedup needs LLM_API_KEY or LLM_BASE_URL")
        result = await group_results(storage, _goal_from_row(goal_row), grouper, model=settings.llm_model)
        lines += [
            f"  run:        {run_dir}",
            f"  goal:       {goal_id}",
            (
                f"  results:    {result.get('items', result['sources'])} items from "
                f"{result['sources']} pages -> {result['groups']} groups"
            ),
        ]
    except Exception as exc:
        failed = True
        lines = [f"dedup failed: {args.task_id}", f"  error:      {exc}", "  existing analyses and groups retained"]
        logger.error("dedup failed: %s", exc)
    finally:
        if storage is not None:
            await storage.close()
        await close_litellm_clients()
    lines.append(
        f"  tokens:     {budget.used} ({budget.input_tokens} in / {budget.output_tokens} out), {budget.calls} calls"
    )
    lines.extend(
        _stage_lines(
            {
                "tokens_by_stage": {
                    name: {
                        "used": u.used,
                        "calls": u.calls,
                        "in": u.input_tokens,
                        "out": u.output_tokens,
                        "cached": u.cached_input_tokens,
                        "thinking": u.reasoning_tokens,
                    }
                    for name, u in budget.by_stage.items()
                }
            }
        )
    )
    report = "\n".join(lines)
    print(report)
    write_report(report)
    if failed:
        raise SystemExit(1)
