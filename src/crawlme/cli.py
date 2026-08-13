"""CLI entry point: argparse + print, no third-party framework.

Commands:
  crawl run "<prompt>" [--max-pages N] [--seeds URL,...]
  crawl pause / resume / stop <task-id>
  crawl status <task-id>
  crawl results <task-id> [--export json|csv]
  crawl replay <task-id> [--prompt "..."]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from crawlme.config import Settings
from crawlme.logging import setup_logging
from crawlme.pioneer.sources.base import UrlSource
from crawlme.pioneer.sources.file import FileSource
from crawlme.pioneer.sources.manual import ManualSource
from crawlme.pioneer.sources.rss import RssSource
from crawlme.scheduler.factory import create_scheduler
from crawlme.schemas import CrawlGoal, CrawlTask

logger = logging.getLogger(__name__)


def main() -> None:
    setup_logging(Settings())
    parser = argparse.ArgumentParser(prog="crawl", description="LLM-driven goal-directed crawler")
    sub = parser.add_subparsers(dest="command")

    #: run -------------------------------------------------------------
    run_p = sub.add_parser("run", help="Start a crawl task")
    run_p.add_argument("prompt", help="Crawl goal description")
    run_p.add_argument("--max-pages", type=int, help="Page budget limit (0 = unlimited)")
    run_p.add_argument("--max-tokens", type=int, help="Token budget limit")
    run_p.add_argument("--max-duration", type=int, help="Time limit in seconds")
    run_p.add_argument("--depth-limit", type=int, help="Max depth from seed (default: 5)")
    run_p.add_argument("--draining", action="store_true", help="Crawl until frontier drained (ignores --max-pages)")
    run_p.add_argument("--seeds", help="Comma-separated seed URLs")
    run_p.add_argument("--source", choices=["manual", "file", "rss"], default="manual")
    run_p.add_argument("--source-path", help="File path or RSS URL for seeds")
    run_p.add_argument("--result-dir", help="Result directory (default: results)")
    run_p.add_argument(
        "--embedding",
        choices=["local", "api", "off"],
        default=None,
        help="Semantic ranking provider (default: local; 'off' = rule-only)",
    )
    run_p.add_argument("--embedding-model", default=None, help="Model id, overriding the provider default")
    run_p.add_argument("--ignore-robots", action="store_true", help="Bypass robots.txt checks")
    run_p.add_argument("--domain-budget", type=int, help="Max pages per domain")
    run_p.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "OFF"],
        default=None,
        help="Log verbosity (overrides env LOG_LEVEL)",
    )

    #: pause / resume / stop -------------------------------------------
    for cmd in ("pause", "resume", "stop"):
        p = sub.add_parser(cmd, help=f"{cmd.capitalize()} a running task")
        p.add_argument("task_id", help="Task ID")

    #: status ----------------------------------------------------------
    status_p = sub.add_parser("status", help="Show task progress")
    status_p.add_argument("task_id", help="Task ID")

    #: results ---------------------------------------------------------
    results_p = sub.add_parser("results", help="Export task results")
    results_p.add_argument("task_id", help="Task ID")
    results_p.add_argument("--export", choices=["json", "csv"], help="Export format")

    #: replay (v0.2 stub) ----------------------------------------------
    replay_p = sub.add_parser("replay", help="Re-analyze a completed task (v0.2)")
    replay_p.add_argument("task_id", help="Task ID")
    replay_p.add_argument("--prompt", help="New analysis prompt")

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    asyncio.run(_dispatch(args))


async def _dispatch(args: argparse.Namespace) -> None:
    cmd = args.command

    if cmd == "run":
        await _cmd_run(args)
    elif cmd in ("pause", "resume", "stop"):
        print(f"{cmd}: task state management requires a running daemon (v0.2)")
    elif cmd == "status":
        print(f"status: reading task {args.task_id} (stub: v0.2)")
    elif cmd == "results":
        print(f"results: exporting task {args.task_id} (stub: v0.2)")
    elif cmd == "replay":
        print(f"replay: re-analyzing task {args.task_id} (stub: v0.2)")


async def _cmd_run(args: argparse.Namespace) -> None:
    cfg = Settings()
    # Flags override env/defaults (see config.py for the layering).
    if args.result_dir is not None:
        cfg.result_dir = Path(args.result_dir)
    if args.ignore_robots:
        cfg.ignore_robots = True
    if args.embedding is not None:
        # "off" maps to "" (disabled); otherwise pass the provider through.
        cfg.embedding_provider = args.embedding if args.embedding != "off" else ""
    if args.embedding_model is not None:
        cfg.embedding_model = args.embedding_model
    if args.log_level is not None:
        cfg.log_level = args.log_level
    goal = CrawlGoal(prompt=args.prompt)
    if args.draining:
        if args.max_pages is not None and args.max_pages > 0:
            print("Error: --draining and --max-pages are mutually exclusive", file=sys.stderr)
            sys.exit(1)
        goal.max_pages = 0
    elif args.max_pages is not None:
        goal.max_pages = args.max_pages
    if args.max_tokens is not None:
        goal.max_tokens = args.max_tokens
    if args.max_duration is not None:
        goal.max_duration_sec = args.max_duration
    if args.depth_limit is not None:
        goal.depth_limit = args.depth_limit
    if args.domain_budget is not None:
        goal.domain_budget = args.domain_budget

    source = _build_source(args)
    candidates = await source.discover(goal)
    allowed_domains: set[str] | None = None
    if hasattr(source, "allowed_domains"):
        allowed_domains = source.allowed_domains

    task = CrawlTask(goal_id=goal.goal_id)
    scheduler = create_scheduler(cfg, goal=goal)

    await scheduler.ingest_seeds(goal, candidates, allowed_domains=allowed_domains)

    logger.info(
        "task=%s prompt=%r pages=%d tokens=%d duration=%ds",
        task.task_id,
        args.prompt,
        goal.max_pages,
        goal.max_tokens,
        goal.max_duration_sec,
    )

    try:
        await scheduler.run(goal, task)
    except KeyboardInterrupt:
        logger.info("interrupted: saving checkpoint")
        await scheduler.pause()
    finally:
        logger.info(
            "state=%s reason=%s pages=%d",
            task.state,
            task.stopping_reason or "none",
            scheduler._counters.pages_fetched,
        )


def _build_source(args: argparse.Namespace) -> UrlSource:
    """Create a URL source from CLI arguments."""
    if args.source == "file" and args.source_path:
        return FileSource(args.source_path)
    if args.source == "rss" and args.source_path:
        return RssSource(args.source_path)
    seeds = [s.strip() for s in (args.seeds or "").split(",") if s.strip()]
    return ManualSource(seeds)
