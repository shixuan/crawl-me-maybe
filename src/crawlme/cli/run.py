"""The crawl run command: one end-to-end task from parsed CLI flags.

The cli package's __init__ owns argument parsing and dispatch; this
module owns the run path — settings layering, wiring (budget, ranker,
scheduler, goal enhancer), the crawl itself, and the end-of-run
report.  The sibling command module replay.py does the same for
re-analysis.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
from pathlib import Path
from typing import Any

from crawlme.config import Settings
from crawlme.llm import TokenBudget, close_litellm_clients
from crawlme.logging import setup_logging
from crawlme.pioneer.goal_enhancer import GoalEnhancer
from crawlme.pioneer.ranker.llm import LLMRanker
from crawlme.pioneer.sources.base import UrlSource
from crawlme.pioneer.sources.file import FileSource
from crawlme.pioneer.sources.manual import ManualSource
from crawlme.pioneer.sources.rss import RssSource
from crawlme.scheduler.engine import CrawlScheduler
from crawlme.scheduler.factory import create_scheduler
from crawlme.scheduler.traversal import traversal_for
from crawlme.schemas import CrawlGoal, CrawlTask, spec_fields

logger = logging.getLogger(__name__)


#: Relative windows accepted by --since, in days.  Months and years are
#: the calendar-free approximations a crawl budget can live with.
_SINCE_UNITS = {
    "day": 1,
    "days": 1,
    "week": 7,
    "weeks": 7,
    "month": 30,
    "months": 30,
    "year": 365,
    "years": 365,
}


def _parse_since(text: str) -> datetime.datetime:
    """Read --since as either a relative window or an absolute date.

    Returns an aware UTC cutoff so it compares directly against the
    publication times the extractor pulls off pages.
    """
    raw = text.strip().lower()
    parts = raw.split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1] in _SINCE_UNITS:
        days = int(parts[0]) * _SINCE_UNITS[parts[1]]
        return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"cannot read --since {text!r}, use '1 week' or '2026-08-01'") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


async def cmd_run(args: argparse.Namespace) -> None:
    """Run one crawl task from the parsed ``crawl run`` arguments."""
    cfg = Settings()
    # Flags override env/defaults (see config.py for the layering).
    if args.result_dir is not None:
        cfg.result_dir = Path(args.result_dir)
    if args.ignore_robots:
        cfg.ignore_robots = True
    if args.recall:
        cfg.recall = True
    if args.order is not None:
        cfg.order = args.order
    if args.no_embedding:
        # "" is the disabled provider: rule-only ranking, no model loaded.
        cfg.embedding_provider = ""
    elif args.embedding is not None:
        cfg.embedding_provider = args.embedding if args.embedding != "off" else ""
    if args.embedding_model is not None:
        cfg.embedding_model = args.embedding_model
    if args.analysis == "off":
        cfg.analysis_enabled = False
    if args.analyzer_max_chars is not None:
        cfg.analyzer_max_chars = args.analyzer_max_chars
    if args.fetcher is not None:
        cfg.fetcher = args.fetcher
    if args.feed is not None:
        cfg.source_kind = args.feed
    if args.session is not None:
        # A session implies a browser: asking to crawl as someone and
        # getting plain httpx would silently crawl the logged-out site.
        cfg.browser_storage_state = args.session
        cfg.fetcher = "browser"
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
    # What a traversal decides, unless the run says otherwise.  Both of
    # these used to be a link graph's answer inherited in silence: a
    # per-domain ceiling that on one platform is a total, and a depth of
    # five where a listing and its posts are two.
    traversal = traversal_for(cfg.source_kind)
    goal.depth_limit = args.depth_limit if args.depth_limit is not None else traversal.depth_limit
    goal.domain_budget = args.domain_budget if args.domain_budget is not None else traversal.domain_budget
    if args.since is not None:
        try:
            goal.since = _parse_since(args.since)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

    # Before the run directory exists and before the enhancer spends a
    # call: bad arguments should cost nothing.
    try:
        source = _build_source(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

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
    # The analysis subsystem (analyzer + signals + priors) is built by
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
        # An explicit --since outranks the window the model inferred.
        # The flag is the user stating the window; the model is guessing
        # it from prose.
        if args.since is None:
            goal.since = enhanced.since
        goal.extraction_spec = enhanced.extraction_spec
        logger.info(
            "goal.enhanced statement_len=%d keywords=%d since=%s fields=%s",
            len(enhanced.statement),
            len(enhanced.keywords),
            enhanced.since.isoformat() if enhanced.since else "none",
            ",".join(spec_fields(enhanced.extraction_spec)) or "none",
        )

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

    # Tear down litellm's cached clients while the loop is still alive,
    # so its shutdown noise never prints after the report.
    await close_litellm_clients()
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
    """Create a URL source from CLI arguments.

    Raises ValueError rather than falling back when the arguments do not
    name a source: the older pair let "--source file" without a path
    become an empty manual list, so a typo produced a run that started,
    found nothing, and reported COMPLETED.
    """
    if args.seeds_file:
        return FileSource(args.seeds_file)
    if args.seeds_rss:
        return RssSource(args.seeds_rss)
    if args.source in {"file", "rss"}:
        if not args.source_path:
            raise ValueError(f"--source {args.source} needs --source-path (or use --seeds-{args.source})")
        return FileSource(args.source_path) if args.source == "file" else RssSource(args.source_path)
    seeds = [s.strip() for s in (args.seeds or "").split(",") if s.strip()]
    return ManualSource(seeds)
