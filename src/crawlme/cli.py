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
from typing import Any

from crawlme.config import Settings
from crawlme.llm import TokenBudget, litellm_loaded
from crawlme.logging import setup_logging
from crawlme.pioneer.goal_enhancer import GoalEnhancer
from crawlme.pioneer.ranker.llm import LLMRanker
from crawlme.pioneer.sources.base import UrlSource
from crawlme.pioneer.sources.file import FileSource
from crawlme.pioneer.sources.manual import ManualSource
from crawlme.pioneer.sources.rss import RssSource
from crawlme.scheduler.engine import CrawlScheduler
from crawlme.scheduler.factory import create_scheduler
from crawlme.schemas import CrawlGoal, CrawlTask

logger = logging.getLogger(__name__)


def main() -> None:
    # No logging setup here: _cmd_run configures once after flags are
    # applied (force), and engine.run() re-calls defensively for library
    # users.  Stub commands print, they don't log.
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
    if args.analysis == "off":
        cfg.analysis_enabled = False
    if args.analyzer_max_chars is not None:
        cfg.analyzer_max_chars = args.analyzer_max_chars
    if args.log_level is not None:
        cfg.log_level = args.log_level
    # Reconfigure with the final settings: main() already configured once
    # (env defaults), and setup_logging is idempotent, so without force the
    # --log-level flag would silently never apply.
    setup_logging(cfg, force=True)
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

    task = CrawlTask(goal_id=goal.goal_id)
    # One shared TokenBudget covers every LLM consumer (the ranker and
    # the Goal Enhancer).  It is created before the scheduler because
    # the ranker needs it at construction time; the sink that feeds the
    # BUDGET_TOKENS stop condition is bound right after the scheduler
    # exists.
    budget = TokenBudget(limit=goal.max_tokens)
    llm_ranker = LLMRanker.from_settings(cfg, budget=budget)
    if llm_ranker is not None:
        logger.info("llm.ranker enabled")
    # The feedback subsystem (analyzer + signals + priors) is built by
    # the factory from settings: the CLI just shares the budget.
    scheduler = create_scheduler(cfg, goal=goal, llm_ranker=llm_ranker, budget=budget)
    budget.bind_sink(scheduler.note_tokens_used)
    # The run dir exists now: log to its file from here on, so the
    # Goal Enhancer's early lines land in the file too.
    scheduler.attach_log_file()

    # One LLM call per task: enrich statement, keywords, and the time
    # window.  Inert without credentials, never blocks the crawl.
    enhanced = await GoalEnhancer.from_settings(cfg, budget=budget).enhance(goal)
    if enhanced is not None:
        goal.goal_statement = enhanced.statement
        goal.keywords = enhanced.keywords
        goal.since = enhanced.since
        logger.info(
            "goal.enhanced statement_len=%d keywords=%d since=%s",
            len(enhanced.statement),
            len(enhanced.keywords),
            enhanced.since.isoformat() if enhanced.since else "none",
        )

    source = _build_source(args)
    candidates = await source.discover(goal)
    allowed_domains: set[str] | None = None
    if hasattr(source, "allowed_domains"):
        allowed_domains = source.allowed_domains

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
        # run() never closed the resources on this path; close them so
        # the process can exit instead of hanging on leaked threads.
        await scheduler.aclose()
    finally:
        logger.info(
            "state=%s reason=%s pages=%d tokens=%d",
            task.state,
            task.stopping_reason or "none",
            scheduler._counters.pages_fetched,
            scheduler._counters.tokens_used,
        )

    # litellm caches aiohttp/httpx clients that are only torn down when
    # the event loop closes, and asyncio then logs a scary SSL error
    # after the task is already COMPLETED.  Close them while the loop
    # is still alive, then give the logging worker a beat to drain.
    # Only relevant when litellm was loaded; best-effort because the
    # cleanup helper is a litellm internal.
    if litellm_loaded():
        try:
            from litellm.llms.custom_httpx.async_client_cleanup import close_litellm_async_clients

            await close_litellm_async_clients()  # type: ignore[no-untyped-call]
        except Exception as e:
            logger.debug("llm.shutdown cleanup best-effort failed: %s", e)
        await asyncio.sleep(0.2)

    # Printed last on purpose: the cleanup above emits its own teardown
    # log lines, and the report should be the final word on the
    # terminal, not buried between shutdown noise.
    _print_summary(scheduler, task, budget)
    # The run is over: mute the whole logging tree.  Interpreter
    # teardown still fires litellm's atexit worker (which creates a
    # fresh event loop and logs about its empty queue) and asyncio's
    # loop-close debug records, all of which would otherwise print
    # after the report at DEBUG level.
    logging.getLogger().setLevel(logging.CRITICAL)


def _print_summary(scheduler: CrawlScheduler, task: CrawlTask, budget: TokenBudget) -> None:
    """Print the end-of-run report: the numbers a user cares about.

    The scheduler's summary carries crawl-level tallies (pages,
    candidates, errors, analyses, stage stats); the budget adds the
    per-call token breakdown the engine never sees.
    """
    summary = scheduler.summary()
    if not isinstance(summary, dict):
        return
    summary["state"] = task.state
    summary["reason"] = task.stopping_reason or "none"
    summary["llm_calls"] = budget.calls
    summary["tokens_in"] = budget.input_tokens
    summary["tokens_out"] = budget.output_tokens
    print(_format_summary(summary))


def _format_summary(s: dict[str, Any]) -> str:
    """Render the summary dict as aligned terminal lines."""
    lines = [f"crawl finished: {s.get('state', '?')} ({s.get('reason', 'none')})"]

    pages = f"{s.get('pages_fetched', 0)} fetched"
    if s.get("candidates_discovered"):
        pages += f", {s['candidates_discovered']} links discovered"
    if s.get("candidates_ranked"):
        pages += f", {s['candidates_ranked']} ranked"
    lines.append(f"  pages:      {pages}")

    calls = s.get("llm_calls", 0)
    tokens = f"{s.get('tokens_used', 0)}"
    if calls:
        tokens += f" ({s.get('tokens_in', 0)} in / {s.get('tokens_out', 0)} out), {calls} calls"
    lines.append(f"  tokens:     {tokens}")

    if s.get("embedding_cache_hits") or s.get("embedding_cache_misses"):
        lines.append(
            f"  embeddings: {s.get('embedding_cache_hits', 0)} cache hits, {s.get('embedding_cache_misses', 0)} misses"
        )

    lines.append(f"  errors:     {s.get('fetch_errors', 0)} fetch failures")

    analyses = s.get("analyses") or {}
    if analyses:
        parts = ", ".join(f"{n} {c}" for c, n in sorted(analyses.items(), key=lambda kv: -kv[1]))
        lines.append(f"  analyses:   {sum(analyses.values())} ({parts})")

    if s.get("duration_sec") is not None:
        lines.append(f"  duration:   {s['duration_sec']}s")
    return "\n".join(lines)


def _build_source(args: argparse.Namespace) -> UrlSource:
    """Create a URL source from CLI arguments."""
    if args.source == "file" and args.source_path:
        return FileSource(args.source_path)
    if args.source == "rss" and args.source_path:
        return RssSource(args.source_path)
    seeds = [s.strip() for s in (args.seeds or "").split(",") if s.strip()]
    return ManualSource(seeds)
