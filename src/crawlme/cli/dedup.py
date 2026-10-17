"""Regroup a stored goal's analyses without fetching or analyzing pages."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from crawlme.cli.report import Report, make_report
from crawlme.config import Settings
from crawlme.llm import TokenBudget
from crawlme.logging import setup_logging
from crawlme.persist import Storage
from crawlme.persist.read import find_run_dir, goal_from_row
from crawlme.scheduler.engine import create_engine
from crawlme.scheduler.pipelines.factory import create_dedup

logger = logging.getLogger(__name__)


async def cmd_dedup(args: argparse.Namespace) -> Report:
    settings = Settings()
    if args.result_dir is not None:
        settings.result_dir = Path(args.result_dir)
    if args.log_level is not None:
        settings.log_level = args.log_level
    setup_logging(settings, force=True)
    budget = TokenBudget(limit=args.max_tokens or 0)
    storage = None
    try:
        run_dir, task = await find_run_dir(settings.result_dir, args.task_id)
        if task.get("state") == "RUNNING":
            raise ValueError("task is still marked RUNNING; wait until it finishes before regrouping")
        storage = Storage(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
        await storage.start()
        goal_id = args.goal or task["goal_id"]
        goal_row = await storage.get_goal(goal_id)
        if goal_row is None:
            raise ValueError(f"goal {goal_id} not found in the run database")
        pipeline = create_dedup(settings, storage=storage, budget=budget, required=True)
        result = await create_engine(pipeline).run(goal_from_row(goal_row))
        if result["status"] != "complete":
            raise ValueError(result.get("error", "dedup failed"))
        return make_report(
            {
                "command": "dedup",
                "task_id": args.task_id,
                "usage": budget.dump(),
                "run_dir": run_dir,
                "goal_id": goal_id,
                "dedup": result,
            }
        )
    except Exception as exc:
        logger.error("dedup failed: %s", exc)
        return make_report(
            {
                "command": "dedup",
                "task_id": args.task_id,
                "usage": budget.dump(),
                "error": str(exc),
                "dedup": {"status": "failed"},
            }
        )
    finally:
        if storage is not None:
            await storage.close()
