"""CLI entry point — argparse + print, no third-party framework.

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
import sys
from pathlib import Path

from crawlme.config import Settings
from crawlme.scheduler.engine import CrawlScheduler
from crawlme.schemas import URL, CrawlGoal, CrawlTask


def main() -> None:
    parser = argparse.ArgumentParser(prog="crawl", description="LLM-driven goal-directed crawler")
    sub = parser.add_subparsers(dest="command")

    # -- run -------------------------------------------------------------
    run_p = sub.add_parser("run", help="Start a crawl task")
    run_p.add_argument("prompt", help="Crawl goal description")
    run_p.add_argument("--max-pages", type=int, help="Page budget limit")
    run_p.add_argument("--max-tokens", type=int, help="Token budget limit")
    run_p.add_argument("--max-duration", type=int, help="Time limit in seconds")
    run_p.add_argument("--seeds", help="Comma-separated seed URLs")
    run_p.add_argument("--source", choices=["manual", "file", "rss"], default="manual")
    run_p.add_argument("--source-path", help="File path or RSS URL for seeds")
    run_p.add_argument("--data-dir", default="data", help="Data directory (default: data)")

    # -- pause / resume / stop -------------------------------------------
    for cmd in ("pause", "resume", "stop"):
        p = sub.add_parser(cmd, help=f"{cmd.capitalize()} a running task")
        p.add_argument("task_id", help="Task ID")

    # -- status ----------------------------------------------------------
    status_p = sub.add_parser("status", help="Show task progress")
    status_p.add_argument("task_id", help="Task ID")

    # -- results ---------------------------------------------------------
    results_p = sub.add_parser("results", help="Export task results")
    results_p.add_argument("task_id", help="Task ID")
    results_p.add_argument("--export", choices=["json", "csv"], help="Export format")

    # -- replay (V0.2 stub) ----------------------------------------------
    replay_p = sub.add_parser("replay", help="Re-analyze a completed task (V0.2)")
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
        print(f"{cmd}: task state management requires a running daemon (V0.2)")
    elif cmd == "status":
        print(f"status: reading task {args.task_id} (stub — V0.2)")
    elif cmd == "results":
        print(f"results: exporting task {args.task_id} (stub — V0.2)")
    elif cmd == "replay":
        print(f"replay: re-analyzing task {args.task_id} (stub — V0.2)")


async def _cmd_run(args: argparse.Namespace) -> None:
    cfg = Settings(data_dir=Path(args.data_dir))
    goal = CrawlGoal(prompt=args.prompt)
    if args.max_pages is not None:
        goal.max_pages = args.max_pages
    if args.max_tokens is not None:
        goal.max_tokens = args.max_tokens
    if args.max_duration is not None:
        goal.max_duration_sec = args.max_duration

    seeds = _parse_seeds(args)
    task = CrawlTask(goal_id=goal.goal_id)

    scheduler = CrawlScheduler(settings=cfg)
    await scheduler._storage.start()

    # Push seed URLs into the frontier.
    items = []
    for url_str in seeds:
        url = URL(raw=url_str, canonical=url_str, url_key=url_str)
        from crawlme.schemas import FrontierItem

        items.append(
            FrontierItem(url=url, url_key=url_str, priority=1.0, score_source="seed", reg_domain=url.reg_domain)
        )
    if items:
        await scheduler._frontier.push_batch(items)

    print(f"Task {task.task_id}: {args.prompt}")
    print(f"Seeds: {seeds}")
    print(f"Budget: pages={goal.max_pages} tokens={goal.max_tokens} time={goal.max_duration_sec}s")
    print("---")

    try:
        await scheduler.run(goal, task)
    except KeyboardInterrupt:
        print("\nInterrupted — saving checkpoint...")
        await scheduler.pause()
    finally:
        print(f"State: {task.state}")
        if task.stopping_reason:
            print(f"Stopped: {task.stopping_reason}")
        print(f"Pages fetched: {scheduler._counters.get('pages_fetched', 0)}")


def _parse_seeds(args: argparse.Namespace) -> list[str]:
    if args.seeds:
        return [s.strip() for s in args.seeds.split(",") if s.strip()]
    if args.source == "file" and args.source_path:
        path = Path(args.source_path)
        if path.exists():
            return [line.strip() for line in path.read_text().splitlines() if line.strip()]
    return []
