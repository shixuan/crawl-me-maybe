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

from crawlme.cli import session
from crawlme.cli.inspect import cmd_inspect
from crawlme.cli.replay import cmd_replay
from crawlme.cli.run import cmd_run


def main() -> None:
    # No logging setup here: the command implementations configure
    # after flags are applied.
    parser = argparse.ArgumentParser(prog="crawl", description="LLM-driven goal-directed crawler")
    sub = parser.add_subparsers(dest="command")

    #: run -------------------------------------------------------------
    run_p = sub.add_parser("run", help="Start a crawl task")
    run_p.add_argument("prompt", help="Crawl goal description")
    # Two families, and the names say which is which.  A page budget is
    # what the run may spend; it says nothing about how many answers that
    # buys, and reading it as a target is how "sixty pages" came to mean
    # twenty-two results.
    run_p.add_argument(
        "--max-relevant",
        type=int,
        default=None,
        help="Stop once this many pages have been judged relevant (0 = no target). "
        "May overshoot slightly: analysis lags fetching",
    )
    run_p.add_argument(
        "--page-budget", "--max-pages", type=int, dest="max_pages", help="Pages this run may read (0 = unlimited)"
    )
    run_p.add_argument(
        "--token-budget", "--max-tokens", type=int, dest="max_tokens", help="LLM tokens this run may spend"
    )
    run_p.add_argument(
        "--time-budget", "--max-duration", type=int, dest="max_duration", help="Seconds this run may take"
    )
    run_p.add_argument("--depth-limit", type=int, help="Max depth from seed (default: 5)")
    run_p.add_argument("--draining", action="store_true", help="Crawl until frontier drained (ignores --max-pages)")
    # Where the entry points come from.  One flag per kind, each carrying
    # its own argument, so "I want a file" cannot be said without saying
    # which file -- the older --source/--source-path pair could, and a
    # missing path silently became an empty manual list.
    run_p.add_argument(
        "--seeds",
        help="Comma-separated seed URLs, or the path to a JSON file holding a list of them. "
        "A feed URL is an ordinary seed: whichever adapter recognises the document reads it",
    )
    run_p.add_argument(
        "--allowed-domains",
        help="Comma-separated registrable domains the crawl may not leave",
    )
    run_p.add_argument("--result-dir", help="Result directory (default: results)")
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
        "--session",
        "--cookies",  # the name this shipped under
        dest="session",
        default=None,
        help="Path to a Playwright storage_state JSON, for crawling as a logged-in session",
    )
    run_p.add_argument(
        "--recall",
        action="store_true",
        help="Diagnostic: keep what the ranker rejected, ranked last, so a run can "
        "measure whether the rejections were right. Spends the tail of the budget "
        "on them, so leave it off when you want results rather than an answer "
        "about the ranker",
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

    session.add_arguments(sub)

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
    elif cmd == "session":
        await session.cmd_session(args)
