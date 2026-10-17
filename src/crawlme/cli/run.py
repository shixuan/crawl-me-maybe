"""Build and execute a crawl from CLI arguments, then report its results."""

from __future__ import annotations

import argparse
import importlib.util
import logging
from pathlib import Path

from crawlme.cli.cutoff import read_cutoff
from crawlme.cli.report import Report, make_report
from crawlme.config import Settings
from crawlme.logging import setup_logging
from crawlme.pioneer.sources import create_source, declared_seeds
from crawlme.platforms import ADAPTERS
from crawlme.runtime import CrawlGoal, CrawlTask
from crawlme.scheduler.engine import create_engine
from crawlme.scheduler.monitor import create_monitor
from crawlme.scheduler.pipelines.crawl import CrawlInput
from crawlme.scheduler.pipelines.factory import create_crawl

logger = logging.getLogger(__name__)


async def cmd_run(args: argparse.Namespace) -> Report:
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
            raise ValueError("--draining and --max-pages are mutually exclusive")
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
        goal.since = read_cutoff(args.since, flag="--since")

    # Before the run directory exists and before the enhancer spends a
    # call: bad arguments should cost nothing.
    source = create_source(args.seeds)

    task = CrawlTask(goal_id=goal.goal_id)
    pipeline = create_crawl(
        cfg,
        goal=goal,
        ranking_enabled=True,
        dedup_enabled=getattr(args, "dedup", "on") == "on",
    )
    engine = create_engine(pipeline, monitor=create_monitor(pipeline), checkpoint=pipeline.save_checkpoint)
    # The run dir exists now: log to its file from here on, so the
    # Goal Enhancer's early lines land in the file too.
    pipeline.attach_log_file()

    candidates = await source.discover(goal)
    # The flag is the run stating its scope; a seeds file may also carry
    # one, and the flag outranks it for the same reason --since does.
    allowed_domains: set[str] | None = set(_split(args.allowed_domains)) or None
    if allowed_domains is None:
        allowed_domains = source.allowed_domains

    await pipeline.startup(
        goal,
        candidates,
        allowed_domains=allowed_domains,
        preserve_since=args.since is not None,
    )

    logger.info(
        "looking for: %s",
        args.prompt,
    )

    await engine.run(CrawlInput(goal, task))
    return make_report({**pipeline.summary(), "session": args.session or "", "platform": _walled_platform(args)})


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
    lines = []
    for module, flag in missing:
        extra, purpose = _EXTRAS[module]
        lines.append(f"{flag} needs {module}, which is not installed.")
        lines.append(f"  {purpose} is an optional extra:  pip install 'crawl-me-maybe[{extra}]'")
        if module == "playwright":
            lines.append("  then fetch the browser itself:  playwright install chromium")
    raise ValueError("\n".join(lines))


def _check_session(args: argparse.Namespace) -> None:
    """Validate a supplied session path and require sessions for declared walled seeds."""
    walled = _walled_platform(args)
    if args.session:
        if Path(args.session).is_file():
            return
        message = f"no session file at {args.session}"
        if walled:
            message += f"\n  Make one with:  crawl session {args.session} --feed {walled}"
        raise ValueError(message)
    if not walled:
        return
    raise ValueError(
        f"crawling {walled} needs a session.\n"
        "  Without one this is a logged-out visitor, and a login-walled\n"
        "  platform answers with its login page, not with nothing.\n"
        f"  Make one with:  crawl session ./{walled}-session.json --feed {walled}"
    )


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
    return declared_seeds(args.seeds)


def _split(value: str | None) -> list[str]:
    """Comma-separated, the way every list-shaped flag here is spelled."""
    return [s.strip() for s in (value or "").split(",") if s.strip()]
