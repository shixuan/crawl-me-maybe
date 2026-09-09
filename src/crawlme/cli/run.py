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
from crawlme.digest.feed.base import PageProblem
from crawlme.llm import Stage, TokenBudget, close_litellm_clients
from crawlme.logging import setup_logging
from crawlme.pioneer.goal_enhancer import GoalEnhancer
from crawlme.pioneer.ranker.llm import LLMRanker
from crawlme.pioneer.sources.base import UrlSource
from crawlme.pioneer.sources.file import FileSource
from crawlme.pioneer.sources.manual import ManualSource
from crawlme.scheduler.engine import CrawlScheduler
from crawlme.scheduler.factory import create_scheduler
from crawlme.schemas import CLASSIFICATIONS, CrawlGoal, CrawlTask, spec_fields

logger = logging.getLogger(__name__)


# Relative windows accepted by --since, in days.  Months and years are
# the calendar-free approximations a crawl budget can live with.
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
    if args.enhance_seeds:
        cfg.enhance_seeds = True
    if args.session is not None:
        # The session alone: it says which context the platform is read
        # through, not that everything must be. Credentials mean nothing
        # to the shop an analyser endorses off it.
        cfg.browser_storage_state = args.session
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
    # A platform's candidates share one host, so a per-domain ceiling
    # would cap the whole crawl. Depth is the user's to set: a run can
    # leave the platform now, and the way out is already three levels.
    if args.session and args.domain_budget is None:
        args.domain_budget = 0
        logger.debug("run.platform domain_budget=%d", args.domain_budget)
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
        logger.info("ranking with the LLM")
    # The analysis subsystem (analyzer + signals + priors) is built by
    # the factory from settings: the CLI just shares the budget.
    scheduler = create_scheduler(cfg, goal=goal, llm_ranker=llm_ranker, budget=budget)
    budget.bind_sink(scheduler.note_tokens_used)
    # The run dir exists now: log to its file from here on, so the
    # Goal Enhancer's early lines land in the file too.
    scheduler.attach_log_file()

    # One LLM call per task: enrich statement, keywords, and the time
    # window.  Inert without credentials, never blocks the crawl.
    logger.info("reading the goal with the model")
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
        goal.constraints = enhanced.constraints
        logger.debug(
            "goal.enhanced statement_len=%d keywords=%d fields=%s",
            len(enhanced.statement),
            len(enhanced.keywords),
            ",".join(spec_fields(enhanced.extraction_spec)) or "none",
        )
        # The window in force, not the one that lost. Logging the model's
        # guess while the flag overrode it reported a month for a run
        # that fetched a week, and nothing said otherwise.
        if args.since is not None and enhanced.since and enhanced.since != goal.since:
            logger.info(
                "reading back to %s, as you asked; the prompt suggested %s",
                goal.since.date().isoformat() if goal.since else "no limit",
                enhanced.since.date().isoformat(),
            )
        else:
            logger.info(
                "reading back to %s",
                goal.since.date().isoformat() if goal.since else "no limit",
            )

    candidates = await source.discover(goal)
    # The flag is the run stating its scope; a seeds file may also carry
    # one, and the flag outranks it for the same reason --since does.
    allowed_domains: set[str] | None = set(_split(args.allowed_domains)) or None
    if allowed_domains is None and hasattr(source, "allowed_domains"):
        allowed_domains = source.allowed_domains

    # After the user's own, so theirs are ingested whatever the model
    # says.
    candidates += await scheduler.enhance_seeds(goal, candidates, budget)
    await scheduler.ingest_seeds(goal, candidates, allowed_domains=allowed_domains)

    logger.info(
        "looking for: %s",
        args.prompt,
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
            "%s after %d pages and %d tokens: %s",
            task.state.lower(),
            scheduler.context.progress.pages_fetched,
            scheduler.context.progress.tokens_used,
            task.stopping_reason or "no reason recorded",
        )

    # Tear down litellm's cached clients while the loop is still alive,
    # so its shutdown noise never prints after the report.
    await close_litellm_clients()
    # Printed last on purpose: the cleanup above emits its own teardown
    # log lines, and the report should be the final word on the
    # terminal, not buried between shutdown noise.
    _print_summary(scheduler, task, budget, args)
    code = exit_code(task.stopping_reason)
    # The run is over: mute the whole logging tree.  Interpreter
    # teardown still fires litellm's atexit worker (which creates a
    # fresh event loop and logs about its empty queue) and asyncio's
    # loop-close debug records, all of which would otherwise print
    # after the report at DEBUG level.
    logging.getLogger().setLevel(logging.CRITICAL)
    if code:
        sys.exit(code)


# Stop reasons that mean the crawl was prevented rather than finished.
_REFUSALS = frozenset({"LOGIN_REQUIRED", "RATE_LIMITED", "FATAL"})


def exit_code(stopping_reason: str | None) -> int:
    """0 when the crawl finished, 1 when something refused it.

    A crawl the platform turned away is not a crawl that found nothing,
    and a scheduled job cannot tell those apart from an exit code alone.
    Run weekly, that is the difference between "no new posts this week"
    and "the session expired a month ago".

    Reasons combine, so this asks whether any of them is a refusal: a
    run can exhaust its page budget *and* hit a login wall, and the wall
    is the part worth waking up for.
    """
    return 1 if _REFUSALS.intersection((stopping_reason or "").split("+")) else 0


# What each optional install buys, and what asks for it.  Kept as data
# so the message names the flag the user actually typed rather than a
# package they have never heard of.
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
    # Only when the whole run needs one. Under dispatch a missing install
    # costs the platform pages alone, and the dispatcher says so.
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


def _print_summary(
    scheduler: CrawlScheduler,
    task: CrawlTask,
    budget: TokenBudget,
    args: argparse.Namespace | None = None,
) -> None:
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
    if args is not None:
        summary["session"] = args.session or ""
        summary["platform"] = _walled_platform(args)
    summary["llm_calls"] = budget.calls
    summary["tokens_in"] = budget.input_tokens
    summary["tokens_out"] = budget.output_tokens
    summary["tokens_cached"] = budget.cached_input_tokens
    summary["tokens_thinking"] = budget.reasoning_tokens
    summary["tokens_by_stage"] = {
        name: {
            "used": u.used,
            "calls": u.calls,
            "in": u.input_tokens,
            "out": u.output_tokens,
            "cached": u.cached_input_tokens,
            "thinking": u.reasoning_tokens,
        }
        for name, u in budget.by_stage.items()
    }
    print(_format_summary(summary))


# Between the report's parts. Indentation alone left them reading as
# one block.
_RULE = "-" * 62
# Where a report line's own text starts, past its label.
_INDENT = " " * 14


def _in_order(counts: dict[str, int], vocabulary: tuple[str, ...]) -> list[str]:
    """The keys present, in the vocabulary's order.

    Not by count. A line sorted by size reshuffles between runs, so
    reading two reports side by side means finding each class again
    before comparing it. Anything the vocabulary does not name follows,
    alphabetically, rather than being dropped.
    """
    named = [k for k in vocabulary if k in counts]
    return named + sorted(k for k in counts if k not in vocabulary)


def _stage_lines(s: dict[str, Any]) -> list[str]:
    """The bill split by who spent it.

    A single total cannot be argued with. Knowing that analysis took
    three quarters of it and seed proposal a twentieth says which one
    to turn down.
    """
    stages: dict[str, dict[str, int]] = s.get("tokens_by_stage") or {}
    if not stages:
        return []
    total = sum(u["used"] for u in stages.values())
    order = _in_order(stages, Stage.ORDER)  # type: ignore[arg-type]
    name_w = max(len(name) for name in order)
    used_w = max(len(str(u["used"])) for u in stages.values())
    lines = []
    for i, name in enumerate(order):
        u = stages[name]
        share = f"{u['used'] / total:.0%}" if total else "0%"
        calls = u["calls"]
        lines.append(
            f"{'  by stage:' if i == 0 else '':<14}"
            f"{name:<{name_w}}  {u['used']:>{used_w}} ({share:>3}), "
            f"{calls} call{'' if calls == 1 else 's'}, "
            f"{u['in']} in / {u['out']} out, {u['cached']} cached, {u['thinking']} thinking"
        )
    return lines


def _refusal_advice(s: dict[str, Any]) -> list[str]:
    """What to do about a stop code, where there is something to do.

    The code alone is for a machine. A login that has run out is the one
    ending with an obvious next step, and printing the step is cheaper
    and catches more than checking the file up front would: a session
    the platform revoked looks perfectly valid on disk.
    """
    if "LOGIN_REQUIRED" not in str(s.get("reason", "")):
        return []
    path = s.get("session") or "./session.json"
    platform = s.get("platform") or ""
    feed = f" --feed {platform}" if platform else ""
    return [
        _RULE,
        "the platform asked for a login. Make a fresh session with:",
        f"  crawl session {path}{feed} --force",
    ]


def _own_seed_lines(s: dict[str, Any]) -> list[str]:
    """What each seed the user gave was actually worth.

    In the order they were given, not best first. A reader comes to this
    looking for one address they typed, and a list that reorders itself
    every run has to be searched instead of read.
    """
    seeds = [v for v in (s.get("seeds") or {}).values() if not v.get("proposed")]
    if not seeds:
        return []
    out = [_RULE, "the sources you gave, in the order you gave them:"]
    for seed in seeds:
        found, judged, fetched, wanted, scored, discovered = seed["funnel"]
        out.append(f"  {f'{found} relevant' if found else 'nothing':>12}  {seed['url']}")
        out.append(
            f"{_INDENT}  {discovered} found, {scored} scored, {wanted} wanted, {fetched} fetched, {judged} judged"
        )
        if seed.get("retired"):
            out.append(f"{_INDENT}  stopped reading it: {seed['retired']}")
    return out


def _proposed_seed_lines(s: dict[str, Any]) -> list[str]:
    """The seeds the run named for itself, and what each was worth.

    Nothing keeps them, on purpose: what a source is worth is a fact
    about this goal, and storing it against the address alone is how the
    last piece of cross-run state went wrong. Printing them puts the
    keeping where the judgement is, which is with whoever reads this.
    """
    proposed = s.get("proposed_seeds") or {}
    asked = s.get("seeds_asked")
    if asked is None:
        return []
    # Every proposal costs a fetch whether it survives or not, so the
    # ones turned away are half of what that spend bought. Without them
    # a run that named nine and kept none reads as having done nothing.
    turned_away = [f"                {url}  --  {why}" for url, why in sorted(s.get("rejected_seeds") or [])]
    if not proposed:
        # Silence here is what a run that was never asked looks like,
        # and one run lost its proposals to an empty model reply.
        out = [_RULE]
        if asked:
            out.append(f"the model named {asked} more sources; none survived verification:")
            out.extend(turned_away)
        else:
            out.append("the model was asked for more sources and named none usable (see the log).")
        return out
    out = [_RULE, "seeds this run added for itself, best first:"]
    for url, (why, funnel) in sorted(proposed.items(), key=lambda kv: -kv[1][1][0]):
        found, judged, fetched, wanted, scored, discovered = funnel
        out.append(f"  {f'{found} relevant' if found else 'nothing':>12}  {url}")
        # Without this, one crawled and empty and one the run never got
        # to both said "nothing", and the same question came back three
        # times over.
        # A funnel, so every gap says something: scored short of
        # discovered is a rotation that never reached it, judged short of
        # fetched is a run that stopped before the analyzer answered.
        # Read as one number, "7 pages, wanted 6, nothing" once looked
        # like a content judgement when six were never looked at.
        out.append(
            f"                {discovered} found, {scored} scored, {wanted} wanted, {fetched} fetched, {judged} judged"
        )
        if why:
            out.append(f"                {why}")
    if turned_away:
        out.append(f"  and {len(turned_away)} more it named that did not survive:")
        out.extend(turned_away)
    out.append("  Worth one? Add it to --seeds yourself.")
    return out


def _format_summary(s: dict[str, Any]) -> str:
    """Render the summary dict as aligned terminal lines."""
    lines = [f"crawl finished: {s.get('state', '?')} ({s.get('reason', 'none')})"]

    lines.append(_RULE)
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
    # Cached input costs about a tenth of fresh input, so the totals
    # above are not a bill until this line is read next to them.  Zero
    # against a large tokens_in means the provider reported nothing,
    # not that nothing was cached.
    cached = s.get("tokens_cached", 0)
    if calls:
        tin = s.get("tokens_in", 0)
        share = f" ({cached / tin:.0%} of input)" if tin else ""
        lines.append(f"  cached in:  {cached}{share}")
    # Output spent thinking is billed and then thrown away, so a large
    # share here is the cheapest thing in the run to argue with.
    think = s.get("tokens_thinking", 0)
    if calls:
        tout = s.get("tokens_out", 0)
        share = f" ({think / tout:.0%} of output)" if tout else ""
        lines.append(f"  thinking:   {think}{share}")
    lines.extend(_stage_lines(s))

    lines.append(f"  errors:     {s.get('fetch_errors', 0)} fetch failures")

    retired = s.get("retired_seeds") or []
    if retired:
        counts: dict[str, int] = {}
        for why in retired:
            counts[why] = counts.get(why, 0) + 1
        lines.append(f"  retired:    {len(retired)} sources stopped paying off")
        lines.extend(
            f"{_INDENT}{why} ({counts[why]} source{'' if counts[why] == 1 else 's'})" for why in sorted(counts)
        )

    analyses = s.get("analyses") or {}
    if analyses:
        parts = ", ".join(f"{analyses[c]} {c}" for c in _in_order(analyses, CLASSIFICATIONS))
        lines.append(f"  analyses:   {sum(analyses.values())} ({parts})")
        # Fetching outruns analysis, so a run that stops on a target
        # leaves pages it paid for and never judged. One fetched 78 and
        # judged 41, and the gap was nowhere on the page.
        target = s.get("max_relevant", 0)
        found = analyses.get("RELEVANT", 0)
        if target and found > target:
            lines.append(
                f"              {found} relevant against a target of {target}; the extra landed from work already sent"
            )
        unjudged = s.get("pages_fetched", 0) - sum(analyses.values())
        if unjudged > 0:
            lines.append(f"              {unjudged} fetched pages were never judged; the run stopped first")

    # Printed whenever it happened at all: a page that was not content
    # is invisible everywhere else, and "0 relevant" reads very
    # differently once you know nine accounts were gone or refused.
    refused = s.get("not_content") or {}
    if refused:
        kinds = tuple(p.value for p in PageProblem)
        parts = ", ".join(f"{refused[k]} {k}" for k in _in_order(refused, kinds))
        lines.append(f"  no content: {sum(refused.values())} pages ({parts})")

    # An adapter that stops recognising a platform's markup shows up as
    # readable listings holding nothing.  Printed whenever any listing
    # came back empty, because "0 relevant" reads very differently once
    # you know the pages arrived and the parser found nothing on them.
    seen, empty = (s.get("listings") or [0, 0])[:2]
    if empty:
        lines.append(f"  listings:   {seen} read, {empty} held no items")
    # A stale listing looks exactly like a healthy one: the count is
    # normal and nothing refused us. Measured on one account the markup
    # ran up to 37 days behind, so silence here is the whole problem.
    stale = s.get("listings_stale", 0)
    if stale:
        lines.append(f"              {stale} read from markup alone; their newest posts are missing")

    if s.get("duration_sec") is not None:
        lines.append(f"  duration:   {s['duration_sec']}s")
    # After the numbers, which are what a reader came for.
    lines.extend(_refusal_advice(s))
    lines.extend(_own_seed_lines(s))
    lines.extend(_proposed_seed_lines(s))
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
