"""Resume an interrupted crawl from its last committed checkpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from crawlme.checkpoint import VERSION
from crawlme.cli.report import Report, make_report
from crawlme.config import Settings
from crawlme.logging import setup_logging
from crawlme.persist import Lock, Storage
from crawlme.persist.read import find_run_dir
from crawlme.runtime import Goal, Task
from crawlme.scheduler.engine import create_engine
from crawlme.scheduler.monitor import create_monitor
from crawlme.scheduler.pipelines.crawl import CrawlInput
from crawlme.scheduler.pipelines.factory import create_crawl


async def cmd_resume(args: argparse.Namespace) -> Report:
    cfg = Settings()
    root = Path(args.result_dir) if args.result_dir else cfg.result_dir
    run_dir, _ = await find_run_dir(root, args.task_id)
    storage = Storage(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
    lock = Lock(storage.db_path)
    pipeline = None
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
        storage.attach_log_file()
        goal = Goal.model_validate(payload["goal"])
        task = Task.model_validate(payload["task"])
        if task.task_id != args.task_id or task.goal_id != goal.goal_id:
            raise ValueError("checkpoint does not match this task and goal")
        task.stopping_reason = None
        task.end_at = None
        if (payload["ranking"] or payload["analysis"]) and not (cfg.llm_api_key or cfg.llm_base_url):
            raise ValueError("configure LLM credentials before resuming this run")
        cfg.analysis_enabled = payload["analysis"]
        pipeline = create_crawl(
            cfg,
            goal=goal,
            ranking_enabled=payload["ranking"],
            storage=storage,
            dedup_enabled=payload["dedup"],
        )
        engine = create_engine(pipeline, monitor=create_monitor(pipeline), checkpoint=pipeline.save_checkpoint)
        print(f"resuming: {task.task_id} ({run_dir})")
        await engine.run(CrawlInput(goal, task, checkpoint=payload, lock=lock))
        return make_report({**pipeline.summary(), "session": cfg.browser_storage_state})
    finally:
        try:
            if pipeline is None:
                await storage.close()
        finally:
            lock.release()
