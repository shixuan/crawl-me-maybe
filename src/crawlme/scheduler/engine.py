"""CrawlScheduler: the orchestrator that wires all v0.1 modules together.

Drives two concurrent asyncio tasks (fetch_pump + rank_pump), runs stop-condition
checks each iteration, handles pause / resume / stop, and saves periodic
checkpoints for crash recovery.

v0.1 path (no LLM):
  - Page Analyzer is skipped (v0.2)
  - the feedback subsystem is absent (v0.2)
  - tokens_used is fed externally via note_tokens_used (v0.2)
  - HybridRanker uses RuleRanker only

See docs/arch.md fetch_pump / rank_pump for the pseudocode this follows.
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import logging
import time
from typing import Any

from crawlme.analyzer import Analyzer
from crawlme.config import Settings
from crawlme.digest.extractor import Extractor
from crawlme.digest.feed.base import FeedDependencyError, PageProblem
from crawlme.digest.fetcher import Fetcher
from crawlme.digest.harvest import Harvest, Harvester, PageHarvester
from crawlme.logging import setup_logging
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.pioneer.frontier import Frontier
from crawlme.pioneer.prefilter import PreFilter
from crawlme.pioneer.ranker import Ranker
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.scheduler.stop_conds import check_stop
from crawlme.schemas import (
    URL,
    AnalysisResult,
    Candidate,
    CrawlGoal,
    CrawlTask,
    FetchResult,
    FrontierItem,
    FrontierSnapshot,
    Page,
    RankDecision,
    RankHistorySummary,
)
from crawlme.state.context import CrawlContext, CrawlCounters, RunStats
from crawlme.state.events import EventEmitter, EventType
from crawlme.storage.contracts import CrawlDb

logger = logging.getLogger(__name__)

_CHECKPOINT_INTERVAL = 10
# How many candidates the rank pump takes out of the buffer at once,
# and therefore how long anything waits to become fetchable: nothing in
# a drained batch reaches the frontier until all of it is scored.
#
# It no longer decides coverage.  The buffer hands out a turn from each
# seed, so a drain of any size is the same mix; before that it was
# first-come-first-served and the size was the only thing standing
# between one loud account and the whole run.
#
# What is left is latency against call count, and the measured rates
# settle it: scoring supplies about twenty candidates a minute and
# fetching consumes about nine, so there is no shortage of supply to
# buy with a bigger batch.  There is a shortage of *early* supply --
# fetching cannot start until the first batch lands -- so this is sized
# to one of the ranker's own calls, which its character budget puts at
# roughly twenty.
_RANK_BATCH_SIZE = 20
#: How many judged-relevant pages the ranker is reminded of.  Matches the
#: ranker's own cap: keeping more here would only be sliced off there.
_SEEN_SO_FAR = 5
_POP_SLEEP = 0.2

#: How long a stopping run waits for the fetches already in the air.
#: Each is bounded by the fetch and extract timeouts and by one analyzer
#: call, so this is a backstop rather than the usual path: whatever is
#: still running when it expires was never going to finish.
_SETTLE_TIMEOUT = 120.0


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _endorsed_href(link: str) -> str | None:
    """Normalize one analyzer-endorsed link, or reject it.

    Endorsements are copied out of page text by a model, so unlike a
    harvested href they are not guaranteed to be links at all.  A bare
    host resolved against the page it was found on becomes a path on the
    wrong site: a run endorsed "www.mollyteaca.com" from an Instagram
    profile and fetched instagram.com/mollytea_canada/www.mollyteaca.com,
    which Instagram answered 200 for, as it does for any path.  A page
    that does not exist then cost a fetch, an analysis, and a slot in the
    page budget.

    A leading "www." is the one bare host worth rescuing rather than
    dropping: no relative path starts that way.  Anything else has to
    look like a link already.
    """
    href = link.strip()
    if not href:
        return None
    if href.startswith(("http://", "https://", "/")):
        return href
    if href.lower().startswith("www."):
        return f"https://{href}"
    return None


class CrawlScheduler:
    """Orchestrator that wires all v0.1 modules together.

    Accepts only interfaces (Protocols or simple concrete classes).
    Call ``create_scheduler(settings)`` from ``crawlme.scheduler.factory``
    to construct a fully-wired instance with default implementations.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        storage: CrawlDb,
        frontier: Frontier,
        fetcher: Fetcher,
        extractor: Extractor,
        robots: RobotsPolicy,
        prefilter: PreFilter,
        ranker: Ranker | None,
        canonicalizer: Canonicalizer,
        harvester: Harvester | None = None,
        analyzer: Analyzer | None = None,
        context: CrawlContext | None = None,
    ) -> None:
        self._cfg = settings
        self._storage = storage
        self._frontier = frontier
        self._fetcher = fetcher
        self._extractor = extractor
        self._robots = robots
        self._prefilter = prefilter
        self._ranker = ranker
        self._canonicalizer = canonicalizer
        # What a page yields depends on the kind of source it came
        # from: a link graph offers its links, a feed listing offers
        # post permalinks. Defaults to links so a bare scheduler
        # behaves as it always did.
        self._harvester: Harvester = harvester or PageHarvester(canonicalizer)
        # The analyzer, or None when the subsystem is off.  It reads a
        # fetched page and returns a verdict with the evidence behind
        # it; the endorsed links it names are the only way a crawl
        # leaves the platform it started on, so they are collected here
        # and injected at the next enqueue.
        self._analyzer = analyzer
        self._endorsed: collections.deque[tuple[str, str]] = collections.deque()
        # The pages judged relevant so far, newest last, for the ranker's
        # "seen so far" section.  Bounded, because the prompt shows only
        # the last few and an unbounded list would grow for a whole run
        # to be sliced away every time.
        self._relevant_pages: collections.deque[dict[str, Any]] = collections.deque(maxlen=_SEEN_SO_FAR)
        # The run context: one mutable object holding the stop-condition
        # counters and the report statistics.  The factory injects it;
        # a bare scheduler creates its own so tests stay cheap.
        self._ctx = context or CrawlContext(counters=CrawlCounters(), stats=RunStats())
        if analyzer is not None:
            # Every successful analysis persists and counts here,
            # including ones that only succeeded on a background retry.
            analyzer.bind_sink(self._on_analysis)

        self._fetch_sem = asyncio.Semaphore(settings.fetch_concurrency)
        self._llm_sem = asyncio.Semaphore(settings.llm_concurrency)

        self._state: str = "CREATED"
        # LLM usage that landed before run() recreated the counters
        # (the Goal Enhancer runs first).  run() seeds the fresh
        # counters from this so the total survives the reset.
        self._tokens_used_start = 0
        self._goal: CrawlGoal | None = None
        self._task: CrawlTask | None = None
        self._pump_tasks: list[asyncio.Task[None]] = []
        # Fetches in the air.  A set the tasks remove themselves from,
        # so it holds exactly what is still running.
        self._inflight: set[asyncio.Task[None]] = set()
        # Maps url_key -> {title, link_count} so the ranker can use per-page
        # signals (title_match F3 + position F7) instead of defaulting to 0.5.
        self._page_contexts: dict[str, dict[str, Any]] = {}
        # canonical URL -> url_key, for endorsed links to inherit their
        # source page's depth.
        self._url_key_of: dict[str, str] = {}
        # url_key -> the seed it descends from, so a candidate found on a
        # page inherits that page's seed rather than pointing at the page
        # itself.  Grouping on the immediate parent would make fairness
        # mean "a turn from every page fetched", which a crawl generates
        # itself and without bound.
        self._seed_of: dict[str, str] = {}
        self._events: EventEmitter | None = None

    #: seed ingestion --------------------------------------------------

    async def ingest_seeds(
        self,
        goal: CrawlGoal,
        candidates: list[Candidate],
        allowed_domains: set[str] | None = None,
    ) -> int:
        """Canonicalize, pre-filter, and enqueue seed candidates.

        Seeds only go through a subset of PreFilter rules (dedup, blacklist,
        protocol, scope): we intentionally skip robots/extension/url-pattern/
        depth/domain-budget since these are user-provided entry points.
        """
        ctx = self._frontier.get_prefilter_context(
            allow_fetch=lambda url: True,  # seeds bypass robots
            allowed_domains=allowed_domains,
        )
        items: list[FrontierItem] = []
        n_ingested = 0
        for c in candidates:
            url = self._canonicalizer.canonicalize(c.url.raw, c.url.raw)
            c.url = url
            decision, _ = self._prefilter.check(c, goal, ctx)
            if decision.value != "allow":
                logger.debug("seed.rejected url=%s reason=%s", url.raw, _)
                continue
            items.append(
                FrontierItem(
                    url=url,
                    url_key=url.url_key,
                    priority=1.0,
                    score_source="seed",
                    reg_domain=url.reg_domain,
                    seed_url_key=url.url_key,
                )
            )
            n_ingested += 1
        if items:
            await self._frontier.push_batch(items)
        if self._events and n_ingested > 0:
            self._events.emit(EventType.URL_DISCOVERED, {"source": "seed", "count": n_ingested})
        self._counters.seed_count += n_ingested
        logger.info("ingest.seeds total=%d ingested=%d", len(candidates), n_ingested)
        return n_ingested

    #: public API -------------------------------------------------------

    def attach_log_file(self) -> None:
        """Start writing logs to the run dir's log file.

        Called by the CLI before the Goal Enhancer runs, so those early
        logs land in the file, not just the terminal.
        """
        self._storage.attach_log_file()

    def note_tokens_used(self, total: int) -> None:
        """Feed the shared LLM token counter from the outside.

        The TokenBudget sink calls here after every LLM call, so the
        BUDGET_TOKENS stop condition sees fresh numbers while the pumps
        run.  Usage recorded before run() survives its _counters reset
        through _tokens_used_start.
        """
        self._tokens_used_start = total
        self._counters.tokens_used = total

    async def run(self, goal: CrawlGoal, task: CrawlTask) -> None:
        self._goal = goal
        self._task = task
        self._state = "RUNNING"
        task.state = "RUNNING"

        setup_logging(self._cfg)
        logger.info(
            "task.start task_id=%s pages=%d tokens=%d duration=%ds",
            task.task_id,
            goal.max_pages,
            goal.max_tokens,
            goal.max_duration_sec,
        )

        await self._storage.start()
        self._events = EventEmitter(self._storage, task.task_id)
        self._events.emit(EventType.TASK_STARTED, {"goal_id": goal.goal_id, "prompt": goal.prompt[:200]})

        self._ctx.reset(goal=goal, tokens_used_start=self._tokens_used_start)
        # Whether "no recent content" may ever read as "no more content".
        # A link graph's pages arrive in no order, so a streak means
        # nothing there either -- but the streak is already gated on a
        # single entry point, and one walk of one site is ordered often
        # enough to be worth reading.  A platform run interleaves
        # accounts, so it never is.
        self._counters.time_horizon_allowed = not self._cfg.browser_storage_state
        # Persist goal (with its enhanced statement / keywords / since)
        # and task rows so replay and introspection have a record.
        self._storage.save_goal(goal.model_dump(mode="json"))
        self._storage.save_task(task.model_dump(mode="json"))

        self._pump_tasks = [
            asyncio.create_task(self._fetch_pump()),
            asyncio.create_task(self._rank_pump()),
        ]
        await asyncio.gather(*self._pump_tasks, return_exceptions=True)
        await self._settle_inflight()

        task.state = "COMPLETED"
        task.end_at = _utcnow()
        reason = task.stopping_reason or "none"
        self._reconcile()
        logger.info(
            "task.done task_id=%s pages=%d tokens=%d reason=%s",
            task.task_id,
            self._counters.pages_fetched,
            self._counters.tokens_used,
            reason,
        )
        if self._events:
            self._events.emit(
                EventType.STOPPED,
                {"reason": reason, "pages_fetched": self._counters.pages_fetched},
            )
        # Final task row: state, counters, and the stop reason.
        task.counters = {"tokens_used": self._counters.tokens_used, "pages_fetched": self._counters.pages_fetched}
        self._storage.save_task(task.model_dump(mode="json"))
        # Deliberately not a finally: on KeyboardInterrupt the CLI runs
        # pause() (which checkpoints through this storage) before its
        # own aclose(), so resources must still be open here.
        await self.aclose()

    async def _settle_inflight(self) -> None:
        """Let the fetches already in the air finish before anything closes.

        The pumps returning is not the run being over: a fetch is its own
        task, and each one still has a page to save and an analysis to
        record.  One run stopped with seven of them running, closed the
        storage and the analyzer underneath, and ended with seven pages
        fetched, saved, and never analysed -- and with the analyzer's
        retries for them still arriving in the log after the crawl had
        reported itself complete.
        """
        if not self._inflight:
            return
        pending = set(self._inflight)
        logger.info("task.settling in_flight=%d", len(pending))
        _, still = await asyncio.wait(pending, timeout=_SETTLE_TIMEOUT)
        if still:
            # Past the backstop: whatever is left was not going to
            # finish, and holding the process open for it is worse.
            logger.warning("task.settle_timeout abandoned=%d", len(still))
            for t in still:
                t.cancel()
            await asyncio.gather(*still, return_exceptions=True)

    async def aclose(self) -> None:
        """Release stage-owned resources (ranker, storage)."""
        if self._analyzer is not None:
            # Closes the analyzer's retry queue (hang-safe exit, see the
            # aiosqlite worker-thread lesson).
            await self._analyzer.aclose()
        if self._ranker is not None:
            await self._ranker.aclose()
        await self._fetcher.aclose()
        await self._storage.close()

    def _on_analysis(self, result: AnalysisResult) -> None:
        """Analyzer sink: persist, tally, and keep the endorsed links."""
        self._storage.save_analysis(result.model_dump(mode="json"))
        by_class = self._ctx.stats.analyses_by_class
        by_class[result.classification] = by_class.get(result.classification, 0) + 1
        fb = result.feedback
        if fb.endorsed_links and fb.url:
            self._endorsed.extend((link, fb.url) for link in fb.endorsed_links)
        # What the ranker is told about the run so far.  This is the
        # analysis half of the loop: ranking predicts, analysis
        # establishes, and what analysis established goes back into the
        # next prediction.  It reached the prompt through the steering
        # facade until v0.3.0 removed it, and the prompt kept reading a
        # list nothing filled any more.
        if result.classification == "RELEVANT":
            self._relevant_pages.append(
                {
                    "url": fb.url,
                    "title": fb.title,
                    "relevance": round(result.relevance_score, 2),
                }
            )
        # Backfill the judgment into the source page's context so the LLM
        # ranker can tell a link off a RELEVANT article from a link off a
        # help page.  Retries land here through the same sink, so a late
        # success still informs whatever is ranked after it.  Candidates
        # ranked before it keep the old behavior.
        self._record_page_context(
            result.url_key,
            {
                "classification": result.classification,
                "relevance": result.relevance_score,
                "summary": result.summary or "",
            },
        )
        # The only place a page is ever judged, so the only place the
        # relevance window can be fed.  DIMINISHING_RETURNS reads it to
        # decide whether the crawl has stopped finding anything.
        relevant = result.relevance_score >= self._counters.relevance_threshold
        self._counters.relevance_window.append(relevant)
        # The same judgement answers both questions the run asks: the
        # window says whether this is still working, the tally says
        # whether it is enough.
        self._counters.relevant_found += relevant

    def _note_page_age(self, page: Page) -> None:
        """Track how many pages in a row fell outside the goal's window.

        Only pages that state a publication time move the streak.  A page
        that says nothing is not evidence in either direction, so it
        neither advances nor resets it.
        """
        counters = self._counters
        if counters.since is None or page.published_at is None:
            return
        if page.published_at < counters.since:
            counters.stale_streak += 1
        else:
            counters.stale_streak = 0

    def _record_page_context(self, url_key: str, fields: dict[str, Any]) -> None:
        """Merge per-page context that the ranker reads at rank time.

        Always a merge, never a replace.  The analyzer sink and the
        link-extraction step both write here and they run in that order,
        so assigning a fresh dict would drop the page's judgment.
        """
        if not url_key:
            return
        self._page_contexts.setdefault(url_key, {}).update(fields)

    def _note_not_content(self, problem: PageProblem) -> None:
        """Record a page that was not content, and stop if it was about us.

        The first refusal aimed at the crawl wins: later ones say the
        same thing, and overwriting would report whichever arrived last
        rather than what actually ended the run.
        """
        stats = self._ctx.stats
        stats.not_content[problem.value] = stats.not_content.get(problem.value, 0) + 1
        if problem.refuses_the_run and not self._counters.refused_by:
            self._counters.refused_by = problem.value
            logger.warning("crawl.refused problem=%s pages=%d", problem.value, self._counters.pages_fetched)

    def summary(self) -> dict[str, Any]:
        """End-of-run statistics for the CLI's terminal report.

        Everything reads from the run context; stages record into it
        as they work, so no merge step is needed here.
        """
        counters = self._ctx.counters
        stats = self._ctx.stats
        report: dict[str, Any] = {
            "pages_fetched": counters.pages_fetched,
            "tokens_used": counters.tokens_used,
            "candidates_discovered": stats.links_discovered,
            "candidates_ranked": stats.candidates_ranked,
            "fetch_errors": stats.fetch_errors,
            "analyses": dict(stats.analyses_by_class),
        }
        if counters.started_at:
            report["duration_sec"] = round(time.monotonic() - counters.started_at, 1)
        if stats.not_content:
            report["not_content"] = dict(stats.not_content)
        if counters.listings_seen:
            report["listings"] = [counters.listings_seen, counters.listings_empty]
        return report

    @property
    def context(self) -> CrawlContext:
        """The run context: the CLI reads it for the terminal report."""
        return self._ctx

    @property
    def _counters(self) -> CrawlCounters:
        """Stop-condition counters; they live inside the run context."""
        return self._ctx.counters

    @_counters.setter
    def _counters(self, counters: CrawlCounters) -> None:
        self._ctx.counters = counters

    async def pause(self) -> None:
        logger.info("pause.requested inflight=%d", self._counters.in_flight)
        self._state = "PAUSING"
        # Wait for in-flight fetches to finish.
        while self._counters.in_flight > 0:
            await asyncio.sleep(0.1)
        self._state = "PAUSED"
        if self._task:
            self._task.state = "PAUSED"
            await self._checkpoint()
        if self._events:
            self._events.emit(EventType.TASK_PAUSED)
        logger.info("pause.done")

    async def resume(self) -> None:
        if self._state != "PAUSED":
            return
        # Restore from latest checkpoint.
        snap = await self._load_latest_snapshot()
        if snap:
            logger.info(
                "resume.restored heap=%d pending=%d visited=%d", len(snap.heap), len(snap.pending), len(snap.visited)
            )
            self._frontier.restore(snap)
        else:
            logger.warning("resume.no_snapshot")
        self._state = "RUNNING"
        if self._task:
            self._task.state = "RUNNING"
        if self._events:
            self._events.emit(EventType.TASK_RESUMED)
        self._counters.started_at = time.monotonic()
        self._pump_tasks = [
            asyncio.create_task(self._fetch_pump()),
            asyncio.create_task(self._rank_pump()),
        ]
        await asyncio.gather(*self._pump_tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._state = "STOPPING"
        if self._task:
            self._task.state = "STOPPING"

    def _reconcile(self) -> None:
        """Say what the run had against what it read, at the end.

        A run that stops holding work reports success exactly like one
        that finished: a missing session gave a COMPLETED with no pages,
        a per-domain ceiling gave one with a hundred and sixty
        candidates still queued, and a rank batch that landed after the
        last fetch gave one that never opened the account it was asked
        about.  None of them said so anywhere.

        Whether leaving work behind is wrong depends on the reason -- a
        page budget is supposed to stop early -- so this states the
        numbers and leaves the judgement to whoever reads them, loudly
        enough to be seen.
        """
        left_frontier = self._frontier.size
        left_buffer = self._frontier.waiting_size
        stats = self._ctx.stats
        logger.info(
            "task.reconcile discovered=%d ranked=%d fetched=%d left_in_frontier=%d left_in_buffer=%d",
            stats.links_discovered,
            stats.candidates_ranked,
            self._counters.pages_fetched,
            left_frontier,
            left_buffer,
        )
        if left_frontier or left_buffer:
            logger.warning(
                "task.unfinished %d candidates were never read (frontier=%d buffer=%d)",
                left_frontier + left_buffer,
                left_frontier,
                left_buffer,
            )

    def _record_stop_reason(self) -> None:
        """Name why the run is ending, for a path that bypassed the check."""
        if self._task is None or self._task.stopping_reason:
            return
        reasons = check_stop(self._task, self._frontier, self._counters)
        self._task.stopping_reason = "+".join(r.code for r in reasons) if reasons else "FRONTIER_DRAINED"
        logger.info("stop.on_exit reason=%s pages=%d", self._task.stopping_reason, self._counters.pages_fetched)

    #: fetch loop -------------------------------------------------------

    async def _fetch_pump(self) -> None:
        while self._state == "RUNNING":
            await self._inject_endorsed()
            reasons = check_stop(
                self._task,  # type: ignore[arg-type]
                self._frontier,
                self._counters,
            )
            if reasons:
                codes = "+".join(r.code for r in reasons)
                self._task.stopping_reason = codes  # type: ignore[union-attr]
                self._state = "STOPPING"
                logger.info(
                    "stop.triggered reasons=%s pages=%d frontier=%d buffer=%d inflight=%d",
                    codes,
                    self._counters.pages_fetched,
                    self._frontier.size,
                    self._frontier.waiting_size,
                    self._counters.in_flight,
                )
                await self._frontier.waiting.wake()
                break

            # Page budget gates pops, not just completions: committed
            # in-flight fetches count against it, otherwise the pump keeps
            # popping while fetches are in the air and overshoots max_pages
            # by up to fetch_concurrency-1 (check_stop only sees landed
            # pages).  Failed in-flight fetches release their slot, so we
            # wait here rather than break.
            if (
                self._counters.max_pages > 0
                and self._counters.pages_fetched + self._counters.in_flight >= self._counters.max_pages
            ):
                await asyncio.sleep(_POP_SLEEP)
                continue

            item = await self._frontier.pop_next(
                now=_utcnow(),
                next_allowed=None if self._cfg.ignore_robots else self._robots.next_allowed_at,
                global_budget=self._counters.max_pages,
            )
            if item is None:
                if self._frontier.scoring > 0:
                    # A rank call holds the only candidates there are.
                    # The rank pump is inside it, so it is neither asleep
                    # nor able to act on a wake: waiting is the whole job.
                    await asyncio.sleep(_POP_SLEEP)
                    continue
                if self._frontier.waiting.is_empty:
                    # A pop that returns nothing is not the same as an
                    # empty frontier: an item whose cooldown has not
                    # expired stays queued and comes back on its own.
                    # Reading the first as the second ended a run at zero
                    # pages with its only seed still waiting.  Items a
                    # gate refuses outright are not a reason to wait,
                    # because nothing about them will change.
                    if self._counters.in_flight == 0 and self._frontier.cooling == 0:
                        # The loop leaves here without going back to the
                        # stop check at the top, so the reason has to be
                        # recorded on the way out or the run reports
                        # "completed" with no cause at all.
                        self._record_stop_reason()
                        logger.info("fetch_pump.exhausted frontier=0 buffer=0")
                        await self._frontier.waiting.wake()
                        break
                    # Nothing waiting to be scored, but either a fetch is
                    # in the air or an item is cooling down, so more work
                    # is coming.  Wake the rank pump in case it is blocked
                    # on wait_until so it can observe state changes.
                    await self._frontier.waiting.wake()
                elif self._frontier.size == 0 and self._counters.in_flight == 0:
                    # Buffer has items but nothing is fetching: the rank
                    # pump may be asleep on a stale predicate (frontier was
                    # non-empty when it last checked).  Wake it.
                    logger.debug(
                        "fetch_pump.waking_rank frontier=%d buffer=%d", self._frontier.size, self._frontier.waiting_size
                    )
                    await self._frontier.waiting.wake()
                await asyncio.sleep(_POP_SLEEP)
                continue

            self._counters.in_flight = self._counters.in_flight + 1
            # Held, not fired and forgotten.  Unheld, these outlived the
            # run: the pumps returned, aclose() shut the storage and the
            # analyzer, and seven tasks went on writing into both.
            task = asyncio.create_task(self._handle_fetch(item))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

        self._state = "STOPPING"

    async def _inject_endorsed(self) -> None:
        """Push analyzer-endorsed links straight into the frontier.

        The analyzer "would click" these links itself, so they skip the
        ranking funnel and enter at full priority.  They still pass the
        prefilter (dedup, scope, robots, depth), so an endorsement can
        never override the crawler's hard rules.
        """
        if self._goal is None or not self._endorsed:
            return
        endorsed = list(self._endorsed)
        self._endorsed.clear()
        if not endorsed:
            return
        ctx = self._frontier.get_prefilter_context(
            allow_fetch=lambda url: self._robots.allow_fetch(url),
        )
        items: list[FrontierItem] = []
        for link, source_url in endorsed:
            usable = _endorsed_href(link)
            if usable is None:
                logger.debug("endorsed.unusable link=%r source=%s", link[:80], source_url)
                continue
            url = self._canonicalizer.canonicalize(usable, source_url)
            source_key = self._url_key_of.get(source_url, "")
            source_depth = int(self._page_contexts.get(source_key, {}).get("depth", 0))
            candidate = Candidate(url=url, depth=source_depth + 1, discovered_at=_utcnow())
            decision, _ = self._prefilter.check(candidate, self._goal, ctx)
            if decision.value != "allow":
                continue
            items.append(
                FrontierItem(
                    url=url,
                    url_key=url.url_key,
                    priority=1.0,
                    score_source="endorsed",
                    depth=source_depth + 1,
                    reg_domain=url.reg_domain,
                    # A shop's own site endorsed from an account belongs
                    # to that account's share, not to a share of its own.
                    seed_url_key=self._seed_of.get(source_key, source_key),
                )
            )
        if items:
            await self._frontier.push_batch(items)
            logger.info("endorsed.injected count=%d", len(items))

    async def _fetch_and_extract(self, item: FrontierItem) -> tuple[FetchResult, Page] | None:
        """Download and parse one page while holding a fetch slot.

        The slot covers the network request and the parse it feeds,
        and nothing else.  Returns None when the item is finished with,
        either because it failed or because extraction timed out.
        """
        async with self._fetch_sem:
            try:
                result = await self._fetcher.fetch(item)
                domain = item.url.reg_domain or _extract_domain(item.url.canonical)
                self._robots.record_response(domain, result.status_code)
            except Exception as e:
                logger.warning("fetch.failed url_key=%s domain=%s depth=%d", item.url_key, item.reg_domain, item.depth)
                if self._events:
                    self._events.emit(EventType.FETCH_FAILED, {"url_key": item.url_key, "depth": item.depth})
                self._storage.save_error(
                    {
                        "task_id": self._task.task_id if self._task else "",
                        "url_key": item.url_key,
                        "stage": "fetch",
                        "error_type": type(e).__name__,
                        "attempt": item.attempts,
                        "created_at": _utcnow().isoformat(),
                    }
                )
                self._ctx.stats.fetch_errors += 1
                await self._frontier.record_outcome(item, "FAILED")
                return None

            # Extract content: offload to thread pool with a timeout.
            raw_path = self._storage.raw_html_path(item.url_key, result.item_id)
            logger.info("fetch.extracting url_key=%s size=%dKB", item.url_key, len(result.raw) // 1024)
            await asyncio.to_thread(self._storage.save_raw_html, item.url_key, result.item_id, result.raw)
            try:
                page = await asyncio.wait_for(
                    asyncio.to_thread(self._extractor.extract, result, raw_path),
                    timeout=self._cfg.extract_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("fetch.extract_timeout url_key=%s size=%dKB", item.url_key, len(result.raw) // 1024)
                await self._frontier.record_outcome(item, "SKIPPED")
                self._counters.pages_fetched = self._counters.pages_fetched + 1
                return None
            page.payload_paths = await asyncio.to_thread(self._save_payloads, item.url_key, result)
            self._storage.save_page(page)
            self._note_page_age(page)
            return result, page

    def _save_payloads(self, url_key: str, result: FetchResult) -> list[str]:
        """Store what the page fetched for itself, beside the page.

        Same treatment the raw HTML gets: the frozen copy is what a
        harvester reads back, so a parser can be changed and rerun
        against exactly what arrived.
        """
        paths: list[str] = []
        for i, payload in enumerate(result.payloads):
            try:
                paths.append(self._storage.save_payload(url_key, result.item_id, i, payload.body))
            except OSError:
                logger.warning("fetch.payload_unsaved url_key=%s index=%d", url_key, i, exc_info=True)
        return paths

    async def _handle_fetch(self, item: FrontierItem) -> None:
        if self._events:
            self._events.emit(EventType.FETCH_STARTED, {"url_key": item.url_key, "depth": item.depth})
        try:
            fetched = await self._fetch_and_extract(item)
            if fetched is None:
                return
            result, page = fetched

            # One LLM call per page: classification, summary, and the
            # the fields it was asked to extract.  Failures park
            # on the analyzer's own retry queue and never block this loop.
            #
            # Deliberately outside the fetch slot.  Waiting on an LLM
            # while holding one made fetch_concurrency and llm_concurrency
            # nested rather than independent, so the inner limit throttled
            # the outer one and HTTP fetching stalled behind analysis.
            #
            # It stays ahead of link extraction, though: the ranker reads
            # this page's verdict out of the page context when it scores
            # the links found below (2.9).
            if self._analyzer is not None:
                assert self._goal is not None
                await self._analyzer.analyze(page, self._goal)
            if self._events:
                self._events.emit(
                    EventType.FETCH_COMPLETED,
                    {"url_key": item.url_key, "status": result.status_code, "size": len(result.raw)},
                )
                self._events.emit(
                    EventType.PAGE_EXTRACTED,
                    {"url_key": page.url_key, "title": page.title, "status": page.extraction_status},
                )

            # Extract links -> Candidates -> PreFilter -> Buffer.
            # Bounded like the extraction step: a pathological page
            # must lose its links, not stall the whole crawl.
            try:
                harvest = await asyncio.wait_for(
                    asyncio.to_thread(self._harvester.harvest, page, item.depth),
                    timeout=self._cfg.extract_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("fetch.link_timeout url_key=%s size=%dKB", item.url_key, len(result.raw) // 1024)
                harvest = Harvest([])
            except FeedDependencyError as e:
                # Not about this page: every later page of the same
                # format fails identically, so carrying on would spend
                # the whole budget producing nothing and report success.
                logger.error("fetch.adapter_dependency url_key=%s: %s", item.url_key, e)
                self._counters.fatal_error = str(e)
                return
            candidates = harvest.candidates
            if harvest.problem is not None:
                self._note_not_content(harvest.problem)
            if harvest.listing:
                self._counters.listings_seen += 1
                self._counters.listings_empty += int(not candidates)
            # Every candidate belongs to the seed its page belonged to,
            # however many hops back.  Recorded here because this is the
            # only place that holds both ends of the link.
            seed = self._seed_of.get(item.url_key, item.seed_url_key or item.url_key)
            for c in candidates:
                c.seed_url_key = seed
            self._ctx.stats.links_discovered += len(candidates)
            logger.debug(
                "extracted url_key=%s title=%r links=%d status=%s",
                page.url_key,
                page.title,
                len(candidates),
                page.extraction_status,
            )

            # Record page context for the ranker, plus the URL and
            # depth an endorsed link is resolved against.
            self._record_page_context(
                page.url_key,
                {
                    "title": page.title or "",
                    "link_count": len(candidates),
                    "url": page.url.canonical,
                    "depth": item.depth,
                },
            )
            self._url_key_of[page.url.canonical] = page.url_key
            self._seed_of[page.url_key] = seed
            ctx = self._frontier.get_prefilter_context(
                allow_fetch=lambda url: self._robots.allow_fetch(url),
            )
            n_allowed = 0
            n_filtered = 0
            for c in candidates:
                decision, _ = self._prefilter.check(c, self._goal, ctx)  # type: ignore[arg-type]
                if decision.value == "allow":
                    c.status = "BUFFERED"
                    await self._frontier.push_candidates([c])
                    n_allowed += 1
                    self._storage.save_link(c)
                else:
                    c.status = "FILTERED_OUT"
                    n_filtered += 1
                # Progress pulse: large pages take a while to persist.
                total = n_allowed + n_filtered
                if total % 500 == 0:
                    logger.info("fetch.progress url_key=%s candidates=%d/%d", page.url_key, total, len(candidates))
            logger.debug(
                "prefilter url_key=%s total=%d allowed=%d filtered=%d",
                page.url_key,
                len(candidates),
                n_allowed,
                n_filtered,
            )
            if self._events and n_allowed > 0:
                self._events.emit(
                    EventType.URL_DISCOVERED,
                    {"source_url_key": page.url_key, "count": n_allowed, "filtered": n_filtered},
                )

            await self._frontier.record_outcome(item, "COMPLETED")

            self._counters.pages_fetched = self._counters.pages_fetched + 1
            n = self._counters.pages_fetched
            logger.info(
                "fetch.ok #%d url_key=%s title=%r links=%d allowed=%d elapsed=%.1fs",
                n,
                page.url_key,
                page.title,
                len(candidates),
                n_allowed,
                (time.monotonic() - self._counters.started_at),
            )

            # Periodic checkpoint.
            if self._counters.pages_fetched % _CHECKPOINT_INTERVAL == 0:
                await self._checkpoint()

        finally:
            self._counters.in_flight = max(0, self._counters.in_flight - 1)

    #: rank loop --------------------------------------------------------

    async def _rank_pump(self) -> None:
        ranked_total = 0
        while self._state == "RUNNING":
            logger.debug("rank_pump.wait frontier=%d buffer=%d", self._frontier.size, self._frontier.waiting_size)
            await self._frontier.waiting.wait_until(
                lambda: self._frontier.waiting.ready(self._frontier.size == 0) or self._state != "RUNNING"
            )
            logger.debug(
                "rank_pump.woke frontier=%d buffer=%d state=%s",
                self._frontier.size,
                self._frontier.waiting_size,
                self._state,
            )
            if self._state != "RUNNING":
                break

            batch = await self._frontier.take_for_ranking(_RANK_BATCH_SIZE)
            if not batch:
                continue

            logger.debug("rank_pump.drain batch=%d frontier=%d", len(batch), self._frontier.size)
            try:
                await self._rank_and_enqueue(batch)
            finally:
                self._frontier.finish_ranking(len(batch))
                ranked_total += len(batch)
                self._ctx.stats.candidates_ranked = ranked_total

    async def _rank_and_enqueue(self, batch: list[Candidate]) -> None:
        """Score one drained batch and push what survives to the frontier."""
        history = RankHistorySummary(
            goal=self._goal.prompt if self._goal else "",
            relevant_pages=list(self._relevant_pages),
        )
        assert self._goal is not None
        if self._ranker is None:
            # Without credentials there is no ranker.  The frontier
            # already hands candidates out a turn per seed, oldest
            # first, so passing them through at one flat priority keeps
            # that order rather than inventing one.
            decisions = [
                RankDecision(
                    candidate_id=c.candidate_id,
                    url_key=c.url.url_key,
                    priority=0.5,
                    dropped=False,
                    ranker="none",
                    rationale="no ranker configured",
                    decided_at=_utcnow(),
                )
                for c in batch
            ]
        else:
            decisions = await self._ranker.rank_batch(self._goal, batch, history, page_contexts=self._page_contexts)

        n_dropped = sum(1 for d in decisions if d.dropped)
        n_kept = len(decisions) - n_dropped
        logger.info(
            "rank.batch candidates=%d kept=%d dropped=%d",
            len(batch),
            n_kept,
            n_dropped,
        )

        items: list[FrontierItem] = []
        for d in decisions:
            self._storage.save_rank_decision(d)
            if d.dropped:
                continue
            c = _find_candidate(batch, d.candidate_id)
            depth = c.depth if c else 0
            reg_domain = c.url.reg_domain if c else ""
            items.append(
                FrontierItem(
                    url=c.url if c else URL(raw="", canonical="", url_key=d.url_key),
                    url_key=d.url_key,
                    priority=d.priority,
                    score_source=d.ranker,
                    rationale=d.rationale,
                    depth=depth,
                    reg_domain=reg_domain,
                    seed_url_key=(c.seed_url_key or "") if c else "",
                )
            )
        await self._frontier.push_batch(items)
        if self._events and items:
            self._events.emit(
                EventType.CANDIDATE_ENQUEUED,
                {"count": len(items), "dropped": n_dropped},
            )

    #: checkpoint -------------------------------------------------------

    async def _checkpoint(self) -> None:
        if self._task is None:
            return
        snap = self._frontier.snapshot(task_id=self._task.task_id)
        snap_id = f"{self._task.task_id}-latest"
        snap_dict = snap.model_dump(mode="json")
        self._storage.save_snapshot(
            {
                "snapshot_id": snap_id,
                "task_id": snap.task_id,
                "snapshot_json": snap_dict,
                "created_at": _utcnow().isoformat(),
            }
        )
        if self._events:
            self._events.emit(EventType.CHECKPOINT_SAVED, {"pages": self._counters.pages_fetched})

    async def _load_latest_snapshot(self) -> FrontierSnapshot | None:
        if self._task is None:
            return None
        snap_id = f"{self._task.task_id}-latest"
        row = await self._storage.get_snapshot(snap_id)
        if row is None:
            return None
        snap_json = row.get("snapshot_json", {})
        if isinstance(snap_json, str):
            import json

            snap_json = json.loads(snap_json)
        # JSON serializes set -> list; restore to set for FrontierSnapshot.
        if "visited" in snap_json and isinstance(snap_json["visited"], list):
            snap_json["visited"] = set(snap_json["visited"])
        return FrontierSnapshot(**snap_json)


#: helpers -------------------------------------------------------------


def _extract_domain(raw_url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(raw_url).hostname or "").lower()


def _find_candidate(batch: list[Candidate], candidate_id: str) -> Candidate | None:
    for c in batch:
        if c.candidate_id == candidate_id:
            return c
    return None
