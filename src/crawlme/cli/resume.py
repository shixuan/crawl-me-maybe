"""Resume an interrupted crawl from its last committed checkpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from crawlme.checkpoint import VERSION
from crawlme.cli.replay import find_run_dir
from crawlme.config import Settings
from crawlme.llm import TokenBudget, close_litellm_clients
from crawlme.logging import setup_logging
from crawlme.pioneer.ranker.llm import LLMRanker
from crawlme.scheduler.factory import create_scheduler
from crawlme.schemas import CrawlGoal, CrawlTask
from crawlme.storage.lock import RunLock
from crawlme.storage.sqlite import SqliteStorage


async def cmd_resume(args: argparse.Namespace) -> None:
    cfg = Settings()
    root = Path(args.result_dir) if args.result_dir else cfg.result_dir
    run_dir, _ = await find_run_dir(root, args.task_id)
    storage = SqliteStorage(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
    lock = RunLock(storage.db_path)
    scheduler = None
    try:
        lock.acquire()
        await storage.start()
        task_row = await storage.get_task(args.task_id)
        if task_row is None or task_row["state"] not in {"RUNNING", "INTERRUPTED", "FAILED"}:
            raise ValueError("only interrupted or failed runs can be resumed")
        row = await storage.get_checkpoint(f"{args.task_id}-latest")
        payload = json.loads(row["checkpoint_json"]) if row else {}
        if payload.get("version") != VERSION:
            raise ValueError("no supported recovery checkpoint; start a new crawl")
        cfg = Settings(**{**cfg.model_dump(), **payload["settings"], "result_dir": root})
        if args.log_level:
            cfg.log_level = args.log_level
        setup_logging(cfg, force=True)
        goal = CrawlGoal.model_validate(payload["goal"])
        task = CrawlTask.model_validate(payload["task"])
        if task.task_id != args.task_id or task.goal_id != goal.goal_id:
            raise ValueError("checkpoint does not match this task and goal")
        task.stopping_reason = None
        task.end_at = None
        budget = TokenBudget(limit=goal.max_tokens)
        ranker = LLMRanker.from_settings(cfg, budget=budget) if payload["ranking"] else None
        if (payload["ranking"] and ranker is None) or (
            payload["analysis"] and not (cfg.llm_api_key or cfg.llm_base_url)
        ):
            raise ValueError("configure LLM credentials before resuming this run")
        cfg.analysis_enabled = payload["analysis"]
        scheduler = create_scheduler(
            cfg, goal=goal, llm_ranker=ranker, budget=budget, storage=storage, dedup_enabled=payload["dedup"]
        )
        budget.bind_sink(scheduler.note_tokens_used)
        print(f"resuming: {task.task_id} ({run_dir})")
        await scheduler.run(goal, task, checkpoint=payload, lock=lock)
        print(f"{task.state.lower()}: {task.task_id}; {budget.used} recorded tokens")
        if task.state == "FAILED":
            raise SystemExit(1)
    except ValueError as exc:
        print(f"resume failed: {exc}")
        raise SystemExit(1) from exc
    finally:
        try:
            if scheduler is not None:
                await scheduler.aclose()
            else:
                await storage.close()
            await close_litellm_clients()
        finally:
            lock.release()
