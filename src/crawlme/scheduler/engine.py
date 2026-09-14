"""Coordinate fetching, analysis, discovery and ranking through injected components."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from crawlme.config import Settings
from crawlme.digest.feed.base import FeedDependencyError
from crawlme.llm import TokenBudget
from crawlme.logging import setup_logging
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.pioneer.frontier import Frontier
from crawlme.pioneer.prefilter import PreFilter, PreFilterContext
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.scheduler.reporting import summary
from crawlme.scheduler.stop_conds import check_stop, why_retire
from crawlme.scheduler.workers import (
    AnalysisWorker,
    DiscoveryWorker,
    FetchedPage,
    FetchFailure,
    FetchWorker,
    RankingWorker,
)
from crawlme.schemas import (
    AnalysisResult,
    Candidate,
    CrawlGoal,
    CrawlTask,
    FrontierItem,
    FrontierSnapshot,
    Page,
)
from crawlme.state.context import CrawlContext
from crawlme.state.events import EventEmitter, EventType
from crawlme.state.tracking import RunTracking
from crawlme.storage.contracts import CrawlDb

SeedEnhancement = Callable[
    [CrawlGoal, list[str], TokenBudget | None],
    Awaitable[tuple[list[Candidate], int, list[tuple[str, str]]]],
]

logger = logging.getLogger(__name__)

_CHECKPOINT_INTERVAL = 10


# Maximum candidates per rank-pump batch; smaller batches release fetchable work sooner.
_RANK_BATCH_SIZE = 20
_POP_SLEEP = 0.2

# Cap continuation pages per seed independently of traversal depth.
_MAX_LISTING_PAGES = 5

# Where the next page of a listing enters. Below anything the ranker
# thought well of, above what it demoted.
_LISTING_PAGE_PRIORITY = 0.5

# Maximum wait for dispatched work during shutdown.
_SETTLE_TIMEOUT = 120.0


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class CrawlScheduler:
    """Run concurrent fetch and rank pumps over a shared frontier."""

    def __init__(
        self,
        *,
        settings: Settings,
        storage: CrawlDb,
        frontier: Frontier,
        fetch: FetchWorker,
        ranking: RankingWorker,
        analysis: AnalysisWorker,
        discovery: DiscoveryWorker,
        robots: RobotsPolicy,
        prefilter: PreFilter,
        canonicalizer: Canonicalizer,
        tracking: RunTracking,
        seed_enhancer: SeedEnhancement,
    ) -> None:
        self._cfg = settings
        self._storage = storage
        self._frontier = frontier
        self._fetch = fetch
        self._ranking = ranking
        self._analysis = analysis
        self._discovery = discovery
        self._robots = robots
        self._prefilter = prefilter
        self._canonicalizer = canonicalizer
        self._tracking = tracking
        self._seed_enhancer = seed_enhancer
        self._analysis.bind_sink(self._on_analysis)
        self._state = "CREATED"
        self._tokens_used_start = 0
        self._goal: CrawlGoal | None = None
        self._task: CrawlTask | None = None
        self._pump_tasks: list[asyncio.Task[None]] = []
        self._inflight: set[asyncio.Task[None]] = set()
        self._events: EventEmitter | None = None

    # seed ingestion --------------------------------------------------

    async def enhance_seeds(
        self, goal: CrawlGoal, seeds: list[Candidate], budget: TokenBudget | None = None
    ) -> list[Candidate]:
        """Propose and verify additional seeds when enabled."""
        if not self._cfg.enhance_seeds or not seeds:
            return []
        proposed, n_proposed, rejected = await self._seed_enhancer(goal, [c.url.raw for c in seeds], budget)
        self._tracking.seeds_asked = n_proposed
        self._tracking.rejected_seeds = rejected
        for c in proposed:
            # url_key, the shape the page book counts under.
            key = self._canonicalizer.canonicalize(c.url.raw, c.url.raw).url_key
            self._tracking.proposed_seeds[key] = (c.url.canonical, str(c.signals.get("why", "")))
        return proposed

    async def ingest_seeds(
        self,
        goal: CrawlGoal,
        candidates: list[Candidate],
        allowed_domains: set[str] | None = None,
    ) -> int:
        """Canonicalize and filter seeds, then enqueue them at priority 1.0."""
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
                    seed_ext=c.seed_ext,
                )
            )
            self._tracking.seeds[url.url_key].url = url.canonical
            n_ingested += 1
        if items:
            await self._frontier.push_batch(items)
        if self._events and n_ingested > 0:
            self._events.emit(EventType.URL_DISCOVERED, {"source": "seed", "count": n_ingested})
        logger.info("starting from %d seed%s", n_ingested, "" if n_ingested == 1 else "s")
        return n_ingested

    # public API -------------------------------------------------------

    def attach_log_file(self) -> None:
        """Start writing logs to the run dir's log file.

        Called by the CLI before the Goal Enhancer runs, so those early
        logs land in the file, not just the terminal.
        """
        self._storage.attach_log_file()

    def note_tokens_used(self, total: int) -> None:
        """Update progress from the shared token budget, including pre-run usage."""
        self._tokens_used_start = total
        self.context.progress.tokens_used = total

    async def run(self, goal: CrawlGoal, task: CrawlTask) -> None:
        self._goal = goal
        self._task = task
        self._state = "RUNNING"
        task.state = "RUNNING"

        setup_logging(self._cfg)
        logger.info(
            "crawling, at most %d pages or %d tokens or %ds",
            goal.max_pages,
            goal.max_tokens,
            goal.max_duration_sec,
        )

        await self._storage.start()
        self._events = EventEmitter(self._storage, task.task_id)
        self._events.emit(EventType.TASK_STARTED, {"goal_id": goal.goal_id, "prompt": goal.prompt[:200]})

        self.context.reset(goal=goal, tokens_used_start=self._tokens_used_start)
        # Persist goal (with its enhanced statement / keywords / since)
        # and task rows so replay and introspection have a record.
        self._storage.save_goal(goal.model_dump(mode="json"))
        self._storage.save_task(task.model_dump(mode="json"))

        self._pump_tasks = [
            asyncio.create_task(self._fetch_pump()),
            asyncio.create_task(self._rank_pump()),
        ]
        self._note_pump_failures(await asyncio.gather(*self._pump_tasks, return_exceptions=True))
        await self._settle_inflight()

        task.state = "COMPLETED"
        task.end_at = _utcnow()
        reason = task.stopping_reason or "none"
        self._reconcile()
        logger.info(
            "finished after %d pages and %d tokens: %s",
            self.context.progress.pages_fetched,
            self.context.progress.tokens_used,
            reason,
        )
        if self._events:
            self._events.emit(
                EventType.STOPPED,
                {"reason": reason, "pages_fetched": self.context.progress.pages_fetched},
            )
        # Final task row: state, counters, and the stop reason.
        prog = self.context.progress
        task.counters = {"tokens_used": prog.tokens_used, "pages_fetched": prog.pages_fetched}
        self._storage.save_task(task.model_dump(mode="json"))
        # Keep resources open on KeyboardInterrupt so the CLI can checkpoint before closing.
        await self.aclose()

    def _note_pump_failures(self, results: list[Any]) -> None:
        """Record failed pumps as fatal so the run cannot silently lose a stage."""
        for r in results:
            if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                logger.error("pump.died error=%s", r)
                if not self.context.progress.fatal_error:
                    self.context.progress.fatal_error = str(r)

    async def _settle_inflight(self) -> None:
        """Await dispatched tasks before checkpointing or closing their resources."""
        if not self._inflight:
            return
        pending = set(self._inflight)
        logger.debug("task.settling in_flight=%d", len(pending))
        _, still = await asyncio.wait(pending, timeout=_SETTLE_TIMEOUT)
        if still:
            # Past the backstop: whatever is left was not going to
            # finish, and holding the process open for it is worse.
            logger.warning("task.settle_timeout abandoned=%d", len(still))
            for t in still:
                t.cancel()
            await asyncio.gather(*still, return_exceptions=True)

    async def aclose(self) -> None:
        await self._analysis.aclose()
        await self._ranking.aclose()
        await self._fetch.aclose()
        await self._storage.close()

    def _on_analysis(self, result: AnalysisResult) -> None:
        self._storage.save_analysis(result.model_dump(mode="json"))
        self._consider_retirement(self._tracking.analysis(result))

    def _cast_relevance_vote(self, url_key: str) -> None:
        self._consider_retirement(self._tracking.vote(url_key))

    def _note_page_age(self, page: Page, seed: str) -> None:
        self._consider_retirement(self._tracking.published(page, seed))

    def _consider_retirement(self, seed: str | None) -> None:
        if seed is None:
            return
        state = self._tracking.seeds[seed]
        why = why_retire(state.window, state.stale)
        if why is not None and self._tracking.retire(seed, why):
            self._frontier.retire(seed)
            logger.info("done with one source: %s", why)

    def summary(self) -> dict[str, Any]:
        return summary(self._tracking)

    @property
    def context(self) -> CrawlContext:
        """The run context: the CLI reads it for the terminal report."""
        return self._tracking.context

    async def pause(self) -> None:
        """Stop dispatch, settle in-flight work and persist a paused checkpoint."""
        logger.debug("pause.requested inflight=%d", self.context.progress.in_flight)
        self._state = "PAUSING"
        await self._settle_inflight()
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
            logger.debug(
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
        self.context.progress.started_at = time.monotonic()
        self._pump_tasks = [
            asyncio.create_task(self._fetch_pump()),
            asyncio.create_task(self._rank_pump()),
        ]
        self._note_pump_failures(await asyncio.gather(*self._pump_tasks, return_exceptions=True))

    async def stop(self) -> None:
        self._state = "STOPPING"
        if self._task:
            self._task.state = "STOPPING"

    def _reconcile(self) -> None:
        """Report fetched, pending and refused work alongside the stop reason."""
        left_frontier = self._frontier.size
        left_buffer = self._frontier.waiting_size
        ledger = self.context.ledger
        logger.debug(
            "task.reconcile discovered=%d ranked=%d fetched=%d left_in_frontier=%d left_in_buffer=%d",
            ledger.links_discovered,
            ledger.candidates_ranked,
            self.context.progress.pages_fetched,
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

    def _enough_found(self) -> bool:
        """Check the result target before starting another analysis. In-flight calls may overshoot."""
        lim, prog = self.context.limits, self.context.progress
        return lim.max_relevant > 0 and prog.relevant_found >= lim.max_relevant

    def _record_stop_reason(self) -> None:
        """Name why the run is ending, for a path that bypassed the check."""
        if self._task is None or self._task.stopping_reason:
            return
        reasons = check_stop(self._task, self._frontier, self.context.limits, self.context.progress)
        self._task.stopping_reason = "+".join(r.code for r in reasons) if reasons else "FRONTIER_DRAINED"
        logger.debug("stop.on_exit reason=%s pages=%d", self._task.stopping_reason, self.context.progress.pages_fetched)

    # fetch loop -------------------------------------------------------

    async def _fetch_pump(self) -> None:
        while self._state == "RUNNING":
            reasons = check_stop(
                self._task,  # type: ignore[arg-type]
                self._frontier,
                self.context.limits,
                self.context.progress,
            )
            if reasons:
                codes = "+".join(r.code for r in reasons)
                self._task.stopping_reason = codes  # type: ignore[union-attr]
                self._state = "STOPPING"
                logger.info(
                    "stopping: %s, after %d pages (%d still queued, %d in the air)",
                    codes,
                    self.context.progress.pages_fetched,
                    self._frontier.size + self._frontier.waiting_size,
                    self.context.progress.in_flight,
                )
                await self._frontier.waiting.wake()
                break

            # Count dispatched pages against the budget; wait for failed fetches to release slots.
            if (
                self.context.limits.max_pages > 0
                and self.context.progress.pages_fetched + self.context.progress.in_flight
                >= self.context.limits.max_pages
            ):
                await asyncio.sleep(_POP_SLEEP)
                continue

            # Bound dispatched tasks independently of the fetch and analysis semaphores.
            if self.context.progress.in_flight >= self._cfg.fetch_concurrency + 2 * self._cfg.llm_concurrency:
                await asyncio.sleep(_POP_SLEEP)
                continue

            item = await self._frontier.pop_next(
                now=_utcnow(),
                next_allowed=None if self._cfg.ignore_robots else self._robots.next_allowed_at,
                global_budget=self.context.limits.max_pages,
            )
            if item is None:
                if self._frontier.scoring > 0:
                    # A running rank call still holds pending work; wait for it to return.
                    await asyncio.sleep(_POP_SLEEP)
                    continue
                if self._frontier.waiting.is_empty:
                    # Cooling items remain pending work even when no item can be popped now.
                    if self.context.progress.in_flight == 0 and self._frontier.cooling == 0:
                        # Record the stop reason before leaving this loop directly.
                        self._record_stop_reason()
                        logger.debug("fetch_pump.exhausted frontier=0 buffer=0")
                        await self._frontier.waiting.wake()
                        break
                    # Wake ranking to observe state changes while fetching or cooldowns continue.
                    await self._frontier.waiting.wake()
                elif self._frontier.size == 0 and self.context.progress.in_flight == 0:
                    # Wake ranking when buffered work remains but fetching has no candidates.
                    logger.debug(
                        "fetch_pump.waking_rank frontier=%d buffer=%d", self._frontier.size, self._frontier.waiting_size
                    )
                    await self._frontier.waiting.wake()
                await asyncio.sleep(_POP_SLEEP)
                continue

            self.context.progress.in_flight = self.context.progress.in_flight + 1
            # Retain task handles so shutdown can await writes and analysis.
            task = asyncio.create_task(self._handle_fetch(item))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

        self._state = "STOPPING"

    async def _enqueue_next_page(
        self,
        next_url: str,
        item: FrontierItem,
        ctx: PreFilterContext,
        seed: str,
    ) -> None:
        """Queue a capped listing continuation at the same depth and neutral priority."""
        pages = self._tracking.seeds[seed].listing_pages
        if pages >= _MAX_LISTING_PAGES:
            logger.debug("listing.page_cap seed=%s pages=%d", seed, pages)
            return
        url = self._canonicalizer.canonicalize(next_url, item.url.canonical)
        candidate = Candidate(url=url, depth=item.depth, discovered_at=_utcnow())
        decision, why = self._prefilter.check(candidate, self._goal, ctx)  # type: ignore[arg-type]
        if decision.value != "allow":
            logger.debug("listing.next_dropped url=%s reason=%s", url.canonical, why)
            return
        self._tracking.seeds[seed].listing_pages = pages + 1
        await self._frontier.push_batch(
            [
                FrontierItem(
                    url=url,
                    url_key=url.url_key,
                    priority=_LISTING_PAGE_PRIORITY,
                    score_source="listing_page",
                    depth=item.depth,
                    reg_domain=url.reg_domain,
                    seed_url_key=seed,
                )
            ]
        )
        logger.debug("listing.next_page url=%s page=%d", url.canonical, pages + 2)

    async def _fetch_and_extract(self, item: FrontierItem) -> FetchedPage | None:
        outcome = await self._fetch.fetch(item)
        if isinstance(outcome, FetchFailure):
            if outcome.reason == "robots":
                self.context.ledger.robots_blocked += 1
            elif outcome.reason == "extract_timeout":
                self.context.progress.pages_fetched += 1
            else:
                self.context.ledger.fetch_errors += 1
                if self._events:
                    self._events.emit(EventType.FETCH_FAILED, {"url_key": item.url_key, "depth": item.depth})
                self._storage.save_error(
                    {
                        "task_id": self._task.task_id if self._task else "",
                        "url_key": item.url_key,
                        "stage": "fetch",
                        "error_type": outcome.error_type,
                        "attempt": item.attempts,
                        "created_at": _utcnow().isoformat(),
                    }
                )
            await self._frontier.record_outcome(item, "FAILED" if outcome.reason == "fetch" else "SKIPPED")
            return None
        self._note_page_age(outcome.page, self._tracking.pages.seed_of(item.url_key, item.seed_url_key or item.url_key))
        return outcome

    async def _handle_fetch(self, item: FrontierItem) -> None:
        if self._events:
            self._events.emit(EventType.FETCH_STARTED, {"url_key": item.url_key, "depth": item.depth})
        try:
            fetched = await self._fetch_and_extract(item)
            if fetched is None:
                return
            result, page = fetched.result, fetched.page

            assert self._goal is not None
            self._tracking.pages.open(
                page.url_key,
                page.url.canonical,
                self._tracking.pages.seed_of(item.url_key, item.seed_url_key or item.url_key),
            )
            await self._analysis.analyze(page, self._goal, allowed=lambda: not self._enough_found())
            if self._events:
                self._events.emit(
                    EventType.FETCH_COMPLETED,
                    {"url_key": item.url_key, "status": result.status_code, "size": len(result.raw)},
                )
                self._events.emit(
                    EventType.PAGE_EXTRACTED,
                    {"url_key": page.url_key, "title": page.title, "status": page.extraction_status},
                )

            try:
                harvest = await self._discovery.discover(page, item.depth)
            except FeedDependencyError as e:
                # Missing format dependencies stop the run rather than producing empty listings.
                logger.error("fetch.adapter_dependency url_key=%s: %s", item.url_key, e)
                self.context.progress.fatal_error = str(e)
                return
            self._consider_retirement(self._tracking.discovered(page, item, harvest))
            candidates = harvest.candidates
            seed = self._tracking.pages.seed_of(item.url_key, item.seed_url_key or item.url_key)
            ctx = self._frontier.get_prefilter_context(
                allow_fetch=lambda url: self._robots.allow_fetch(url),
            )
            n_allowed = 0
            n_filtered = 0
            for c in candidates:
                decision, _ = self._prefilter.check(c, self._goal, ctx)
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
                    logger.debug("fetch.progress url_key=%s candidates=%d/%d", page.url_key, total, len(candidates))
            logger.debug(
                "prefilter url_key=%s total=%d allowed=%d filtered=%d",
                page.url_key,
                len(candidates),
                n_allowed,
                n_filtered,
            )
            if harvest.next_url:
                await self._enqueue_next_page(harvest.next_url, item, ctx, seed)
            if self._events and n_allowed > 0:
                self._events.emit(
                    EventType.URL_DISCOVERED,
                    {"source_url_key": page.url_key, "count": n_allowed, "filtered": n_filtered},
                )

            await self._frontier.record_outcome(item, "COMPLETED")

            self.context.progress.pages_fetched = self.context.progress.pages_fetched + 1
            n = self.context.progress.pages_fetched
            logger.debug(
                "fetch.ok #%d url_key=%s title=%r links=%d allowed=%d elapsed=%.1fs",
                n,
                page.url_key,
                page.title,
                len(candidates),
                n_allowed,
                (time.monotonic() - self.context.progress.started_at),
            )

            # Periodic checkpoint.
            if self.context.progress.pages_fetched % _CHECKPOINT_INTERVAL == 0:
                await self._checkpoint()

        finally:
            self.context.progress.in_flight = max(0, self.context.progress.in_flight - 1)

    # rank loop --------------------------------------------------------

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
                self.context.ledger.candidates_ranked = ranked_total

    async def _rank_and_enqueue(self, batch: list[Candidate]) -> None:
        assert self._goal is not None
        history, page_contexts = self._tracking.feedback(self._goal, batch)
        ranked = await self._ranking.rank(batch, self._goal, history, page_contexts)
        for decision in ranked.decisions:
            self._storage.save_rank_decision(decision)
        self._tracking.ranked(batch, ranked.decisions)
        await self._frontier.push_batch(ranked.items)
        if self._events and ranked.items:
            self._events.emit(
                EventType.CANDIDATE_ENQUEUED,
                {
                    "count": len(ranked.items),
                    "dropped": sum(d.dropped for d in ranked.decisions),
                },
            )

    # checkpoint -------------------------------------------------------

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
            self._events.emit(EventType.CHECKPOINT_SAVED, {"pages": self.context.progress.pages_fetched})

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
