"""CLI entry point: argparse + dispatch, no third-party framework.

Commands:
  crawl run "<prompt>" [--max-pages N] [--seeds URL,...]
  crawl inspect <task-id> [--goal G] [--export json|csv]
  crawl replay <task-id> [--prompt "..."] [--limit N] [--max-tokens N] [--force]

The command implementations live in this package's sibling modules:
run.py carries the run path, inspect.py the read-only results view,
replay.py re-analysis.  This module only parses flags and hands them
over.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from crawlme.cli.inspect import cmd_inspect
from crawlme.cli.replay import cmd_replay
from crawlme.cli.run import cmd_run
from crawlme.digest.feed import FEEDS


def main() -> None:
    # No logging setup here: the command implementations configure
    # after flags are applied.
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
    run_p.add_argument(
        "--analysis",
        choices=["on", "off"],
        default=None,
        help="Per-page analysis and the steering it feeds; 'off' disables the whole subsystem",
    )
    run_p.add_argument(
        "--analyzer-max-chars",
        type=int,
        default=None,
        help="Page text sent to the analyzer per page, in characters (default: 3000)",
    )
    run_p.add_argument(
        "--since",
        default=None,
        help="Time window, e.g. '1 week' or '2026-08-01'. Skips candidates already "
        "dated outside it; with a single seed, also stops once content ages out",
    )
    run_p.add_argument(
        "--fetcher",
        choices=["http", "browser"],
        default=None,
        help="How to fetch: 'http' (default) or 'browser' for JS-rendered or login-walled pages",
    )
    run_p.add_argument(
        "--feed",
        choices=sorted(FEEDS),
        default=None,
        help="Read the source as a platform feed: a listing yields post permalinks "
        "instead of the links on the page (default: crawl the link graph)",
    )
    run_p.add_argument(
        "--session",
        "--cookies",  # the name this shipped under
        dest="session",
        default=None,
        help="Path to a Playwright storage_state JSON, for crawling as a logged-in session",
    )
    run_p.add_argument("--ignore-robots", action="store_true", help="Bypass robots.txt checks")
    run_p.add_argument("--domain-budget", type=int, help="Max pages per domain")
    run_p.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "OFF"],
        default=None,
        help="Log verbosity (overrides env LOG_LEVEL)",
    )

    #: inspect ---------------------------------------------------------
    inspect_p = sub.add_parser("inspect", help="Inspect a task's results")
    inspect_p.add_argument("task_id", help="Task ID")
    inspect_p.add_argument(
        "--goal",
        help="Which goal's analyses to show (default: the task's original goal)",
    )
    inspect_p.add_argument("--export", choices=["json", "csv"], help="Dump the pages-and-analyses join to stdout")

    #: replay ---------------------------------------------------------
    replay_p = sub.add_parser("replay", help="Re-analyze a completed task's pages")
    replay_p.add_argument("task_id", help="Task ID")
    replay_p.add_argument("--prompt", help="New goal statement; analyses are stored under a new goal row")
    replay_p.add_argument("--limit", type=int, help="Re-analyze at most N pages (default: all)")
    replay_p.add_argument("--max-tokens", type=int, help="Token budget for this replay (default: unlimited)")
    replay_p.add_argument(
        "--analyzer-max-chars",
        type=int,
        default=None,
        help="Page text sent to the analyzer per page, in characters (default: 3000)",
    )
    replay_p.add_argument(
        "--force",
        action="store_true",
        help="Re-analyze pages that already have an identical analysis",
    )
    replay_p.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "OFF"],
        default=None,
        help="Log verbosity (overrides env LOG_LEVEL)",
    )

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    asyncio.run(_dispatch(args))


async def _dispatch(args: argparse.Namespace) -> None:
    cmd = args.command

    if cmd == "run":
        await cmd_run(args)
    elif cmd == "inspect":
        await cmd_inspect(args)
    elif cmd == "replay":
        await cmd_replay(args)
