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
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

from crawlme.config import Settings
from crawlme.digest.feed import ADAPTERS
from crawlme.llm import TokenBudget, close_litellm_clients
from crawlme.logging import setup_logging
from crawlme.pioneer.goal_enhancer import GoalEnhancer
from crawlme.pioneer.ranker.llm import LLMRanker
from crawlme.pioneer.sources.base import UrlSource
from crawlme.pioneer.sources.file import FileSource
from crawlme.pioneer.sources.manual import ManualSource
from crawlme.scheduler.engine import CrawlScheduler
from crawlme.scheduler.factory import create_scheduler
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
    if args.analysis == "off":
        cfg.analysis_enabled = False
    if args.analyzer_max_chars is not None:
        cfg.analyzer_max_chars = args.analyzer_max_chars
    if args.fetcher is not None:
        cfg.fetcher = args.fetcher
    if args.session is not None:
        # A session implies a browser: asking to crawl as someone and
        # getting plain httpx would silently crawl the logged-out site.
        cfg.browser_storage_state = args.session
        cfg.fetcher = "browser"
    _check_session(args)
    _check_extras(cfg, args)
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
    goal.recall = cfg.recall
    if args.max_relevant is not None:
        goal.max_relevant = args.max_relevant
    if args.max_tokens is not None:
        goal.max_tokens = args.max_tokens
    if args.max_duration is not None:
        goal.max_duration_sec = args.max_duration
    # A session says this run reads a platform, and a platform's answers
    # differ from a link graph's on both counts: every candidate shares
    # one host, so a per-domain ceiling is a total; and a listing and its
    # posts are two levels with no third.  Stated out loud rather than
    # left to a table, and overridable by either flag.
    if args.session:
        if args.depth_limit is None:
            args.depth_limit = 1
        if args.domain_budget is None:
            args.domain_budget = 0
        logger.info("run.platform depth_limit=%d domain_budget=%d", args.depth_limit, args.domain_budget)
    if args.depth_limit is not None:
        goal.depth_limit = args.depth_limit
    if args.domain_budget is not None:
        goal.domain_budget = args.domain_budget
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
    # The flag is the run stating its scope; a seeds file may also carry
    # one, and the flag outranks it for the same reason --since does.
    allowed_domains: set[str] | None = set(_split(args.allowed_domains)) or None
    if allowed_domains is None and hasattr(source, "allowed_domains"):
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


#: What each optional install buys, and what asks for it.  Kept as data
#: so the message names the flag the user actually typed rather than a
#: package they have never heard of.
_EXTRAS = {
    "feedparser": ("rss", "reading feeds"),
    "playwright": ("browser", "crawling with a browser"),
}


def _check_extras(cfg: Settings, args: argparse.Namespace) -> None:
    """Refuse before the crawl when the run needs an install it lacks.

    Both of these used to surface as an ImportError from inside the run:
    the feed one at seed discovery, the browser one at the first fetch,
    by which point the run directory exists and the goal has already
    cost an LLM call.  Neither said what had asked for it.

    Optional on purpose.  A browser is 135MB of package and another
    650MB of Chromium that a link-graph crawl never touches, so making
    every user carry it would be the worse trade -- but then whatever
    needs it has to say so up front.
    """
    wanted: list[tuple[str, str]] = []
    if any(_looks_like_a_feed(u) for u in _declared_seeds(args)):
        wanted.append(("feedparser", "a feed among the seeds"))
    if cfg.fetcher == "browser":
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
    """Refuse the run before it starts, not several hundred pages in.

    Both cases used to surface only once a page came back: a path that
    points at nothing raised inside the fetcher, and no path at all
    crawled the logged-out platform, which looks exactly like a platform
    with nothing on it.  Neither told anyone how to fix it.

    A warning would have been the softer answer for the second one, and
    the wrong one: it scrolls past, and what follows is a whole browser
    run spent fetching login pages.  There is no flag to crawl a feed
    anonymously because nobody has wanted one; the day someone does is
    the day it earns its place.

    A link graph is left alone entirely.  It asks for no session, and
    the advice for making one is feed-shaped, so offering it to a graph
    crawl would point at a command that cannot serve it.
    """
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
    """Whether a seed is worth having feedparser for.

    A guess, and knowingly so: no address says whether it serves a feed,
    measured five wrong in seven.  Being wrong here costs a warning
    nobody needed or an ImportError one page in, which is why the
    adapter itself never guesses -- it reads the document.
    """
    return any(hint in url.lower() for hint in ("rss", "atom", "/feed", "feed.xml", "feeds/"))


def _walled_platform(args: argparse.Namespace) -> str:
    """A platform this run will read that cannot be read logged out.

    Read from the seeds, which is the only thing that ever decided it.
    A flag saying "this is a feed run" could be forgotten while the
    seeds still pointed at the platform, and the run then fetched a few
    hundred login pages, each six hundred kilobytes of markup holding
    nine characters of text.
    """
    for url in _declared_seeds(args):
        for adapter in ADAPTERS:
            if adapter.NEEDS_SESSION and adapter.claims_url(url):
                return str(adapter.PLATFORM)
    return ""


def _declared_seeds(args: argparse.Namespace) -> list[str]:
    """The seeds the command names, without asking the network.

    A feed's entries are not here: reading them costs a request, and the
    check has to happen before the run spends anything.  What a feed
    lists is somebody else's platform anyway.
    """
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

    lines.append(f"  errors:     {s.get('fetch_errors', 0)} fetch failures")

    analyses = s.get("analyses") or {}
    if analyses:
        parts = ", ".join(f"{n} {c}" for c, n in sorted(analyses.items(), key=lambda kv: -kv[1]))
        lines.append(f"  analyses:   {sum(analyses.values())} ({parts})")

    # Printed whenever it happened at all: a page that was not content
    # is invisible everywhere else, and "0 relevant" reads very
    # differently once you know nine accounts were gone or refused.
    refused = s.get("not_content") or {}
    if refused:
        parts = ", ".join(f"{n} {kind}" for kind, n in sorted(refused.items(), key=lambda kv: -kv[1]))
        lines.append(f"  no content: {sum(refused.values())} pages ({parts})")

    # An adapter that stops recognising a platform's markup shows up as
    # readable listings holding nothing.  Printed whenever any listing
    # came back empty, because "0 relevant" reads very differently once
    # you know the pages arrived and the parser found nothing on them.
    seen, empty = (s.get("listings") or [0, 0])[:2]
    if empty:
        lines.append(f"  listings:   {seen} read, {empty} held no items")

    if s.get("duration_sec") is not None:
        lines.append(f"  duration:   {s['duration_sec']}s")
    return "\n".join(lines)


def _build_source(args: argparse.Namespace) -> UrlSource:
    """Seeds are URLs, wherever they are written down.

    A path and a list of addresses are the same information in two
    containers, so they are one flag.  What is not the same is a feed:
    that URL is a seed too, fetched once like any other, and read by
    whichever adapter recognises the document it returns.
    """
    path = _seed_file(args.seeds)
    return FileSource(path) if path is not None else ManualSource(_split(args.seeds))


def _seed_file(value: str | None) -> str | None:
    """The seeds as a path, or None when they are addresses.

    One rule, in one place: anything that does not start with a scheme
    is a file.  A list of addresses and a file holding the same list are
    the same information, so they are the same flag; telling them apart
    has to be done identically wherever it is done.
    """
    text = (value or "").strip()
    if not text or text.lower().startswith(("http://", "https://")):
        return None
    return text


def _split(value: str | None) -> list[str]:
    """Comma-separated, the way every list-shaped flag here is spelled."""
    return [s.strip() for s in (value or "").split(",") if s.strip()]
