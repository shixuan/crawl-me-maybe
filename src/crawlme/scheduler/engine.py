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
import datetime
import logging
import time
from typing import Any

from crawlme.config import Settings
from crawlme.digest.extractor import Extractor
from crawlme.digest.fetcher import Fetcher
from crawlme.digest.links import extract_links
from crawlme.logging import setup_logging
from crawlme.pioneer.buffer import Buffer
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
    FrontierItem,
    FrontierSnapshot,
    Page,
    RankHistorySummary,
)
from crawlme.state.context import CrawlContext, CrawlCounters, RunStats
from crawlme.state.events import EventEmitter, EventType
from crawlme.steering import SteeringSystem
from crawlme.storage.contracts import CrawlDb

logger = logging.getLogger(__name__)

_CHECKPOINT_INTERVAL = 10
_RANK_BATCH_SIZE = 100
_POP_SLEEP = 0.2


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


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
        buffer: Buffer,
        ranker: Ranker,
        canonicalizer: Canonicalizer,
        steering: SteeringSystem | None = None,
        context: CrawlContext | None = None,
    ) -> None:
        self._cfg = settings
        self._storage = storage
        self._frontier = frontier
        self._fetcher = fetcher
        self._extractor = extractor
        self._robots = robots
        self._prefilter = prefilter
        self._buffer = buffer
        self._ranker = ranker
        self._canonicalizer = canonicalizer
        # The optional steering half of the feedback loop (analyzer +
        # run signals + cross-task priors), injected whole by the
        # factory.  The engine only talks to the facade, so None
        # disables the entire subsystem at once.
        self._steering = steering
        # The run context: one mutable object holding the stop-condition
        # counters and the report statistics.  The factory injects it;
        # a bare scheduler creates its own so tests stay cheap.
        self._ctx = context or CrawlContext(counters=CrawlCounters(), stats=RunStats())
        if steering is not None:
            # Every successful analysis persists and counts here,
            # including ones that only succeeded on a background retry.
            steering.bind_sink(self._on_analysis)

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
        # Maps url_key -> {title, link_count} so the ranker can use per-page
        # signals (title_match F3 + position F7) instead of defaulting to 0.5.
        self._page_contexts: dict[str, dict[str, Any]] = {}
        # canonical URL -> url_key, for endorsed links to inherit their
        # source page's depth.
        self._url_key_of: dict[str, str] = {}
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
                )
            )
            n_ingested += 1
        if items:
            await self._frontier.push_batch(items)
        if self._events and n_ingested > 0:
            self._events.emit(EventType.URL_DISCOVERED, {"source": "seed", "count": n_ingested})
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
        # Persist goal (with its enhanced statement / keywords / since)
        # and task rows so replay and introspection have a record.
        self._storage.save_goal(goal.model_dump(mode="json"))
        self._storage.save_task(task.model_dump(mode="json"))

        if self._steering is not None:
            # Cross-task domain reputation: seed the in-memory prior so
            # the very first ranking of a fresh task already sees past
            # tasks' learning.
            await self._steering.load()

        self._pump_tasks = [
            asyncio.create_task(self._fetch_pump()),
            asyncio.create_task(self._rank_pump()),
        ]
        await asyncio.gather(*self._pump_tasks, return_exceptions=True)

        task.state = "COMPLETED"
        task.end_at = _utcnow()
        reason = task.stopping_reason or "none"
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

    async def aclose(self) -> None:
        """Release stage-owned resources (ranker cache, storage).

        The embedding cache holds an aiosqlite connection whose worker
        thread would otherwise keep the interpreter alive after the
        crawl, so it must close before the process exits.
        """
        if self._steering is not None:
            # Close the analyzer's retry queue and flush this run's
            # domain-prior contributions to the global feedback DB
            # (hang-safe exit, see the aiosqlite worker-thread lesson).
            await self._steering.aclose()
        await self._ranker.aclose()
        await self._fetcher.aclose()
        await self._storage.close()

    def _on_analysis(self, result: AnalysisResult) -> None:
        """Analyzer sink: persist, tally, and feed the steering loop."""
        self._storage.save_analysis(result.model_dump(mode="json"))
        by_class = self._ctx.stats.analyses_by_class
        by_class[result.classification] = by_class.get(result.classification, 0) + 1
        if self._steering is not None:
            self._steering.update(result.feedback)
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
        if stats.embedding_cache_hits or stats.embedding_cache_misses:
            report["embedding_cache_hits"] = stats.embedding_cache_hits
            report["embedding_cache_misses"] = stats.embedding_cache_misses
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

    #: fetch loop -------------------------------------------------------

    async def _fetch_pump(self) -> None:
        while self._state == "RUNNING":
            await self._inject_endorsed()
            reasons = check_stop(
                self._task,  # type: ignore[arg-type]
                self._frontier,
                self._buffer,
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
                    self._buffer.size,
                    self._counters.in_flight,
                )
                await self._buffer.wake()
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
                if self._buffer.is_empty:
                    if self._counters.in_flight == 0:
                        logger.info(
                            "fetch_pump.exhausted frontier=%d buffer=%d",
                            self._frontier.size,
                            self._buffer.size,
                        )
                        await self._buffer.wake()
                        break
                    # Buffer is empty but in_flight > 0: tasks may produce
                    # new candidates.  Wake the rank pump in case it is
                    # blocked on wait_until so it can observe state changes.
                    await self._buffer.wake()
                elif self._frontier.size == 0 and self._counters.in_flight == 0:
                    # Buffer has items but nothing is fetching: the rank
                    # pump may be asleep on a stale predicate (frontier was
                    # non-empty when it last checked).  Wake it.
                    logger.debug("fetch_pump.waking_rank frontier=%d buffer=%d", self._frontier.size, self._buffer.size)
                    await self._buffer.wake()
                await asyncio.sleep(_POP_SLEEP)
                continue

            self._counters.in_flight = self._counters.in_flight + 1
            asyncio.create_task(self._handle_fetch(item))  # noqa: RUF006

        self._state = "STOPPING"

    async def _inject_endorsed(self) -> None:
        """Push analyzer-endorsed links straight into the frontier.

        The analyzer "would click" these links itself, so they skip the
        ranking funnel and enter at full priority.  They still pass the
        prefilter (dedup, scope, robots, depth), so an endorsement can
        never override the crawler's hard rules.
        """
        if self._steering is None or self._goal is None:
            return
        endorsed = self._steering.take_endorsed()
        if not endorsed:
            return
        ctx = self._frontier.get_prefilter_context(
            allow_fetch=lambda url: self._robots.allow_fetch(url),
        )
        items: list[FrontierItem] = []
        for link, source_url in endorsed:
            url = self._canonicalizer.canonicalize(link, source_url)
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
                )
            )
        if items:
            await self._frontier.push_batch(items)
            logger.info("endorsed.injected count=%d", len(items))

    async def _handle_fetch(self, item: FrontierItem) -> None:
        if self._events:
            self._events.emit(EventType.FETCH_STARTED, {"url_key": item.url_key, "depth": item.depth})
        async with self._fetch_sem:
            try:
                raw_path = ""
                try:
                    result = await self._fetcher.fetch(item)
                    domain = item.url.reg_domain or _extract_domain(item.url.canonical)
                    self._robots.record_response(domain, result.status_code)
                except Exception as e:
                    logger.warning(
                        "fetch.failed url_key=%s domain=%s depth=%d", item.url_key, item.reg_domain, item.depth
                    )
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
                    return

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
                    return
                self._storage.save_page(page)
                self._note_page_age(page)
                # One LLM call per page (v0.2): classification, summary,
                # and feedback signals for the steering system.  Failures
                # park on the analyzer's own retry queue and never block
                # this loop.
                if self._steering is not None:
                    assert self._goal is not None
                    await self._steering.analyze(page, self._goal)
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
                    raw_links = await asyncio.wait_for(
                        asyncio.to_thread(extract_links, page), timeout=self._cfg.extract_timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning("fetch.link_timeout url_key=%s size=%dKB", item.url_key, len(result.raw) // 1024)
                    raw_links = []
                self._ctx.stats.links_discovered += len(raw_links)
                logger.debug(
                    "extracted url_key=%s title=%r links=%d status=%s",
                    page.url_key,
                    page.title,
                    len(raw_links),
                    page.extraction_status,
                )

                # Record page context for ranker (F3 title_match + F7
                # position), plus the URL and depth the steering
                # multipliers need at ranking time.
                self._record_page_context(
                    page.url_key,
                    {
                        "title": page.title or "",
                        "link_count": len(raw_links),
                        "url": page.url.canonical,
                        "depth": item.depth,
                    },
                )
                self._url_key_of[page.url.canonical] = page.url_key
                ctx = self._frontier.get_prefilter_context(
                    allow_fetch=lambda url: self._robots.allow_fetch(url),
                )
                n_allowed = 0
                n_filtered = 0
                for rl in raw_links:
                    url = self._canonicalizer.canonicalize(rl.href, page.url.canonical)
                    c = Candidate(
                        url=url,
                        anchor=rl.anchor,
                        snippet=rl.snippet,
                        parent_heading=rl.parent_heading,
                        position=rl.position,
                        source_url_key=page.url_key,
                        depth=item.depth + 1,
                        discovered_at=_utcnow(),
                    )
                    decision, _ = self._prefilter.check(c, self._goal, ctx)  # type: ignore[arg-type]
                    if decision.value == "allow":
                        c.status = "BUFFERED"
                        await self._buffer.add([c])
                        n_allowed += 1
                        self._storage.save_link(c)
                    else:
                        c.status = "FILTERED_OUT"
                        n_filtered += 1
                    # Progress pulse: large pages take a while to persist.
                    total = n_allowed + n_filtered
                    if total % 500 == 0:
                        logger.info("fetch.progress url_key=%s candidates=%d/%d", page.url_key, total, len(raw_links))
                logger.debug(
                    "prefilter url_key=%s total=%d allowed=%d filtered=%d",
                    page.url_key,
                    len(raw_links),
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
                    len(raw_links),
                    n_allowed,
                    (time.monotonic() - self._counters.started_at),
                )

                # Periodic checkpoint.
                if self._counters.pages_fetched % _CHECKPOINT_INTERVAL == 0:
                    await self._checkpoint()

            finally:
                self._counters.in_flight = max(0, self._counters.in_flight - 1)

    #: rank loop --------------------------------------------------------

    def _apply_steering(self, priority: float, candidate: Candidate | None) -> float:
        """Fold the real-time steering multipliers into a ranked
        priority (ranking.md 第 3 层).

        Hub pages boost their own outlinks; domains with a consistent
        recent record get boosted or penalized.  Without the steering
        facade the priority passes through untouched.
        """
        if self._steering is None or candidate is None:
            return priority
        page = self._page_contexts.get(candidate.source_url_key or "", {})
        source_url = str(page.get("url", ""))
        multiplier = self._steering.hub_multiplier(source_url) * self._steering.domain_multiplier(
            candidate.url.reg_domain
        )
        return round(priority * multiplier, 4)

    async def _rank_pump(self) -> None:
        ranked_total = 0
        while self._state == "RUNNING":
            logger.debug("rank_pump.wait frontier=%d buffer=%d", self._frontier.size, self._buffer.size)
            await self._buffer.wait_until(
                lambda: self._buffer.ready(self._frontier.size == 0) or self._state != "RUNNING"
            )
            logger.debug(
                "rank_pump.woke frontier=%d buffer=%d state=%s",
                self._frontier.size,
                self._buffer.size,
                self._state,
            )
            if self._state != "RUNNING":
                break

            batch = await self._buffer.drain(_RANK_BATCH_SIZE)
            if not batch:
                continue

            logger.debug("rank_pump.drain batch=%d frontier=%d", len(batch), self._frontier.size)

            history = (
                self._steering.summary()
                if self._steering is not None
                else RankHistorySummary(pages_seen=self._counters.pages_fetched)
            )
            history.fetched = self._counters.pages_fetched
            assert self._goal is not None
            decisions = await self._ranker.rank_batch(self._goal, batch, history, page_contexts=self._page_contexts)

            n_dropped = sum(1 for d in decisions if d.dropped)
            n_kept = len(decisions) - n_dropped
            ranked_total += len(batch)
            self._ctx.stats.candidates_ranked = ranked_total
            logger.info(
                "rank.batch candidates=%d kept=%d dropped=%d ranked_total=%d",
                len(batch),
                n_kept,
                n_dropped,
                ranked_total,
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
                        priority=self._apply_steering(d.priority, c),
                        score_source=d.ranker,
                        rationale=d.rationale,
                        depth=depth,
                        reg_domain=reg_domain,
                    )
                )
            await self._frontier.push_batch(items)
            if self._events and items:
                self._events.emit(
                    EventType.CANDIDATE_ENQUEUED,
                    {"count": len(items), "dropped": n_dropped},
                )

            # In v0.1, tokens_used is always 0 (no LLM calls).

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
