"""Build and execute a crawl from CLI arguments, then report its results."""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
from pathlib import Path

from crawlme.cli.cutoff import read_cutoff
from crawlme.cli.report import finish
from crawlme.config import Settings
from crawlme.llm import TokenBudget, close_litellm_clients
from crawlme.logging import setup_logging
from crawlme.pioneer.ranker import LLMRanker
from crawlme.pioneer.sources import FileSource, ManualSource, Source
from crawlme.platforms import ADAPTERS
from crawlme.runtime import CrawlGoal, CrawlTask
from crawlme.scheduler.factory import create_scheduler

logger = logging.getLogger(__name__)


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
    if args.analysis == "off":
        cfg.analysis_enabled = False
    if args.analyzer_max_chars is not None:
        cfg.analyzer_max_chars = args.analyzer_max_chars
    if args.fetcher is not None:
        cfg.fetcher = args.fetcher
    if args.expand_seeds:
        cfg.expand_seeds = True
    if args.session is not None:
        # A session supplies browser state without forcing browser fetching for every URL.
        cfg.browser_storage_state = args.session
    _check_session(args)
    _check_extras(cfg, args)
    if args.log_level is not None:
        cfg.log_level = args.log_level
    # Apply final CLI settings even if logging was already configured.
    setup_logging(cfg, force=True)
    goal = CrawlGoal(prompt=args.prompt)
    if args.draining:
        if args.max_pages is not None and args.max_pages > 0:
            print("Error: --draining and --max-pages are mutually exclusive", file=sys.stderr)
            sys.exit(1)
        goal.max_pages = 0
    elif args.max_pages is not None:
        goal.max_pages = args.max_pages
    goal.recall = cfg.recall
    if args.max_relevant is not None:
        goal.max_relevant = args.max_relevant
    if args.max_tokens is not None:
        goal.max_tokens = args.max_tokens
    if args.max_duration is not None:
        goal.max_duration_sec = args.max_duration
    # Disable the default domain ceiling for session-based runs on a shared host.
    if args.session and args.domain_budget is None:
        args.domain_budget = 0
        logger.debug("run.platform domain_budget=%d", args.domain_budget)
    if args.depth_limit is not None:
        goal.depth_limit = args.depth_limit
    if args.domain_budget is not None:
        goal.domain_budget = args.domain_budget
    if args.since is not None:
        try:
            goal.since = read_cutoff(args.since, flag="--since")
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
    # Share one budget across enhancement, ranking and analysis.
    budget = TokenBudget(limit=goal.max_tokens)
    llm_ranker = LLMRanker.from_settings(cfg, budget=budget)
    if llm_ranker is not None:
        logger.info("ranking with the LLM")
    # The factory builds the analyzer with the same shared budget.
    scheduler = create_scheduler(
        cfg, goal=goal, llm_ranker=llm_ranker, budget=budget, dedup_enabled=getattr(args, "dedup", "on") == "on"
    )
    # The run dir exists now: log to its file from here on, so the
    # Goal Enhancer's early lines land in the file too.
    scheduler.attach_log_file()

    candidates = await source.discover(goal)
    # The flag is the run stating its scope; a seeds file may also carry
    # one, and the flag outranks it for the same reason --since does.
    allowed_domains: set[str] | None = set(_split(args.allowed_domains)) or None
    if allowed_domains is None and hasattr(source, "allowed_domains"):
        allowed_domains = source.allowed_domains

    await scheduler.pipeline.startup(
        goal,
        candidates,
        allowed_domains=allowed_domains,
        preserve_since=args.since is not None,
    )

    logger.info(
        "looking for: %s",
        args.prompt,
    )

    try:
        await scheduler.run(goal, task)
    finally:
        logger.info(
            "%s after %d pages and %d tokens: %s",
            task.state.lower(),
            scheduler.run_state.progress.pages_fetched,
            budget.used,
            task.stopping_reason or "no reason recorded",
        )
        await close_litellm_clients()

    finish(scheduler, task, budget, session=args.session or "", platform=_walled_platform(args))


# Map missing dependencies to install instructions.
_EXTRAS = {
    "feedparser": ("rss", "reading feeds"),
    "playwright": ("browser", "crawling with a browser"),
}


def _check_extras(cfg: Settings, args: argparse.Namespace) -> None:
    """Reject explicitly requested features with missing dependencies before startup."""
    wanted: list[tuple[str, str]] = []
    if any(_looks_like_a_feed(u) for u in _declared_seeds(args)):
        wanted.append(("feedparser", "a feed among the seeds"))
    # Explicit browser use can be checked before creating a run.
    if cfg.fetcher == "browser" or args.session:
        wanted.append(("playwright", "--session" if args.session else "--fetcher browser"))
    missing = [(m, flag) for m, flag in wanted if importlib.util.find_spec(m) is None]
    if not missing:
        return
    for module, flag in missing:
        extra, purpose = _EXTRAS[module]
        print(f"Error: {flag} needs {module}, which is not installed.", file=sys.stderr)
        print(f"  {purpose} is an optional extra:  pip install 'crawl-me-maybe[{extra}]'", file=sys.stderr)
        if module == "playwright":
            print("  then fetch the browser itself:  playwright install chromium", file=sys.stderr)
    sys.exit(1)


def _check_session(args: argparse.Namespace) -> None:
    """Validate a supplied session path and require sessions for declared walled seeds."""
    walled = _walled_platform(args)
    if args.session:
        if Path(args.session).is_file():
            return
        print(f"Error: no session file at {args.session}", file=sys.stderr)
        if walled:
            print(f"  Make one with:  crawl session {args.session} --feed {walled}", file=sys.stderr)
        sys.exit(1)
    if not walled:
        return
    print(f"Error: crawling {walled} needs a session.", file=sys.stderr)
    print("  Without one this is a logged-out visitor, and a login-walled", file=sys.stderr)
    print("  platform answers with its login page, not with nothing.", file=sys.stderr)
    print(f"  Make one with:  crawl session ./{walled}-session.json --feed {walled}", file=sys.stderr)
    sys.exit(1)


def _looks_like_a_feed(url: str) -> bool:
    """Recognize common feed URL shapes for an early dependency check."""
    return any(hint in url.lower() for hint in ("rss", "atom", "/feed", "feed.xml", "feeds/"))


def _walled_platform(args: argparse.Namespace) -> str:
    """Find a declared seed platform that requires a saved session."""
    for url in _declared_seeds(args):
        for adapter in ADAPTERS:
            if adapter.NEEDS_SESSION and adapter.claims_url(url):
                return str(adapter.PLATFORM)
    return ""


def _declared_seeds(args: argparse.Namespace) -> list[str]:
    """Read declared seed URLs from flags or a JSON file without network access."""
    path = _seed_file(args.seeds)
    if path is None:
        return _split(args.seeds)
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return []  # _build_source reports this properly a moment later
    if isinstance(data, dict):
        return [u for u in data.get("seeds", []) if isinstance(u, str)]
    return [u for u in data if isinstance(u, str)]


def _build_source(args: argparse.Namespace) -> Source:
    """Build a manual or file source from --seeds."""
    path = _seed_file(args.seeds)
    return FileSource(path) if path is not None else ManualSource(_split(args.seeds))


def _seed_file(value: str | None) -> str | None:
    """Return the seed file path, or None for URL arguments."""
    text = (value or "").strip()
    if not text or text.lower().startswith(("http://", "https://")):
        return None
    return text


def _split(value: str | None) -> list[str]:
    """Comma-separated, the way every list-shaped flag here is spelled."""
    return [s.strip() for s in (value or "").split(",") if s.strip()]
