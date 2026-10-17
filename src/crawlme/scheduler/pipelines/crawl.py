"""Crawl topology and module calls, governed by the engine's admission policy."""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass
from typing import Any

from crawlme.checkpoint import Checkpointer
from crawlme.config import Settings
from crawlme.errors import ErrorHandler, Skipped
from crawlme.llm import TokenBudget
from crawlme.logging import setup_logging
from crawlme.logging.progress import activity
from crawlme.persist import Lock, Storage
from crawlme.pioneer import Candidate, FrontierItem
from crawlme.pioneer.frontier import Frontier
from crawlme.pioneer.goal_enhancer import GoalEnhancer
from crawlme.pioneer.prefilter import PreFilter
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.pioneer.seed_expander import SeedExpander, how_many, verify
from crawlme.runtime import CrawlGoal, CrawlTask, EventEmitter, EventType, RunState, Tracker, spec_fields
from crawlme.scheduler.pipelines import Pipeline, PipelineContext
from crawlme.scheduler.pipelines.dedup import DedupPipeline
from crawlme.scheduler.pipelines.page import PageInput, PagePipeline
from crawlme.scheduler.reporting import summary
from crawlme.scheduler.workers import RankingWorker
from crawlme.urls import Canonicalizer

logger = logging.getLogger(__name__)
_RANK_BATCH_SIZE = 20
_POP_SLEEP = 0.2


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass(frozen=True)
class CrawlInput:
    goal: CrawlGoal
    task: CrawlTask
    checkpoint: dict[str, Any] | None = None
    lock: Lock | None = None


class CrawlPipeline(Pipeline[CrawlInput, CrawlTask]):
    def __init__(
        self,
        *,
        settings: Settings,
        budget: TokenBudget,
        checkpointer: Checkpointer,
        error_handler: ErrorHandler,
        goal_enhancer: GoalEnhancer,
        storage: Storage,
        frontier: Frontier,
        ranking: RankingWorker,
        robots: RobotsPolicy,
        prefilter: PreFilter,
        canonicalizer: Canonicalizer,
        tracking: Tracker,
        dedup: DedupPipeline,
        page: PagePipeline,
        seed_expander: SeedExpander | None = None,
    ) -> None:
        self.error_handler = error_handler
        self._goal_enhancer = goal_enhancer
        self._cfg = settings
        self._budget = budget
        self._checkpointer = checkpointer
        self._task: CrawlTask | None = None
        self._lock: Lock | None = None
        self._own_lock = False
        self._closed = False
        self._storage = storage
        self._frontier = frontier
        self._ranking = ranking
        self._robots = robots
        self._prefilter = prefilter
        self._canonicalizer = canonicalizer
        self._tracking = tracking
        self._dedup = dedup
        self._page = page
        self._seed_expander = seed_expander
        self._goal: CrawlGoal | None = None
        self._task_id = ""
        self._events: EventEmitter | None = None
        self._pump_tasks: list[asyncio.Task[None]] = []
        self._inflight: set[asyncio.Task[None]] = set()
        self._context: PipelineContext

    async def startup(
        self,
        goal: CrawlGoal,
        candidates: list[Candidate],
        *,
        allowed_domains: set[str] | None = None,
        preserve_since: bool = False,
    ) -> None:
        """Keep user seeds recoverable even if startup model calls fail."""
        await self.ingest_seeds(goal, candidates, allowed_domains=allowed_domains)
        try:
            # Enhance the goal when configured; keep the original if enhancement fails.
            logger.info("reading the goal with the model")
            enhancer = self._goal_enhancer
            enhanced = await self.error_handler.call(
                "goal",
                lambda: enhancer.enhance(goal),
                optional=True,
                repeatable=True,
            )
            if enhanced is not None and not isinstance(enhanced, Skipped):
                goal.goal_statement = enhanced.statement
                goal.keywords = enhanced.keywords
                # The explicit publication cutoff overrides the inferred one.
                if not preserve_since:
                    goal.since = enhanced.since
                goal.extraction_spec = enhanced.extraction_spec
                goal.time_policy = enhanced.time_policy
                logger.debug(
                    "goal.enhanced statement_len=%d keywords=%d fields=%s",
                    len(enhanced.statement),
                    len(enhanced.keywords),
                    ",".join(spec_fields(enhanced.extraction_spec)) or "none",
                )
                # Report the effective cutoff when it differs from the inferred one.
                if preserve_since and enhanced.since and enhanced.since != goal.since:
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

            proposed = await self.expand_seeds(goal, candidates)
            if proposed:
                await self.ingest_seeds(goal, proposed, allowed_domains=allowed_domains)
        except Exception as exc:
            # run() saves the initial seeds before surfacing this startup failure.
            self.error_handler.abort(exc, "startup")

    @property
    def run_state(self) -> RunState:
        return self._tracking.state

    @property
    def failure(self) -> str:
        return self.run_state.progress.fatal_error

    def record_failure(self, error: BaseException) -> None:
        self.run_state.record_failure(error)

    @property
    def frontier(self) -> Frontier:
        return self._frontier

    @property
    def budget(self) -> TokenBudget:
        return self._budget

    @property
    def counters(self) -> dict[str, int]:
        return {"pages_fetched": self.run_state.progress.pages_fetched, "tokens_used": self._budget.used}

    def prepare(self, goal: CrawlGoal, task: CrawlTask, context: PipelineContext) -> None:
        self.run_state.reset(goal=goal)
        self._goal, self._task_id = goal, task.task_id
        self._events = EventEmitter(self._storage, task.task_id)
        self._context = context
        self._page.prepare(goal, task.task_id)
        logger.info(
            "crawling, at most %d pages or %d tokens or %ds", goal.max_pages, goal.max_tokens, goal.max_duration_sec
        )

    def attach_log_file(self) -> None:
        self._storage.attach_log_file()

    async def run(self, input: CrawlInput, context: PipelineContext) -> CrawlTask:
        goal, task = input.goal, input.task
        self._own_lock = input.lock is None
        self._lock = input.lock or Lock(self._storage.db_path)
        if self._own_lock:
            self._lock.acquire()
        self._task = task
        self._context = context
        task.state = "RUNNING"
        setup_logging(self._cfg)
        recoverable = input.checkpoint is not None
        try:
            await self._storage.start()
            self.prepare(goal, task, context)
            assert self._events is not None
            self._events.emit(
                EventType.TASK_RESUMED if input.checkpoint is not None else EventType.TASK_STARTED,
                {"goal_id": goal.goal_id, "prompt": goal.prompt[:200]},
            )
            self._checkpointer.bind(goal, task)
            if input.checkpoint is not None:
                await self._checkpointer.restore(input.checkpoint)
            self._storage.save_goal(goal.model_dump(mode="json"))
            self._storage.save_task(task.model_dump(mode="json"))
            await context.checkpoint()
            recoverable = True
            try:
                task.stopping_reason = await context.supervise(self._crawl())
                task.state = "STOPPING"
                logger.info("stopping: %s", task.stopping_reason)
                await self.halt()
                await context.settle(self.settle, "settle")
            finally:
                await context.stop_checkpoints()
                await self.abort()
            await self._dedup.run(goal, context)
            self._finish_run()
        except asyncio.CancelledError:
            task.state = "INTERRUPTED"
            task.stopping_reason = "USER_REQUESTED"
            task.counters = self.counters
            self._storage.save_task(task.model_dump(mode="json"))
            logger.info("interrupted; resume with: crawl resume %s", task.task_id)
            raise
        except Exception as exc:
            self._fail("run", exc)
            self._finish_run()
            if recoverable:
                logger.error("resolve the error, then resume with: crawl resume %s", task.task_id)
        return task

    def _finish_run(self) -> None:
        assert self._task is not None
        task = self._task
        task.state = "FAILED" if self.failure else "COMPLETED"
        task.end_at = _utcnow()
        task.counters = self.counters
        logger.info("finished: %s", task.stopping_reason)
        if self._events:
            self._events.emit(EventType.STOPPED, {"reason": task.stopping_reason, **task.counters})
        self._storage.save_task(task.model_dump(mode="json"))

    def _fail(self, stage: str, error: BaseException) -> None:
        if not self.failure:
            self.record_failure(error)
            logger.error("%s.died error=%s", stage, error)
        if self._task is not None:
            self._task.stopping_reason = "FATAL"
            self._task.state = "FAILED"

    async def save_checkpoint(self) -> None:
        if self._task is None or self._task.state not in {"RUNNING", "STOPPING"} or self.failure:
            return
        await self._checkpointer.save()
        if self._events:
            self._events.emit(EventType.CHECKPOINT_SAVED, self.counters)

    async def _crawl(self) -> None:
        self.error_handler.check()
        for work in list(self.run_state.work.values()):
            self._dispatch_page(FrontierItem.model_validate(work["item"]))
        self._page.start()
        self._pump_tasks = [asyncio.create_task(self._fetch_pump()), asyncio.create_task(self._rank_pump())]
        await self.error_handler.watch(asyncio.gather(*self._pump_tasks))

    async def halt(self) -> None:
        """Stop producing new work; keep dispatched pages available for settling."""
        for task in self._pump_tasks:
            task.cancel()
        results = await asyncio.gather(*self._pump_tasks, return_exceptions=True)
        self._pump_tasks.clear()
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result

    def _page_done(self, task: asyncio.Task[None]) -> None:
        self._inflight.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self.error_handler.abort(error, "page")

    async def settle(self) -> None:
        await self._context.settle(self._settle_pages, "pages")
        await self._page.settle(self._context)

    async def _settle_pages(self) -> None:
        if self._inflight:
            # Ending the wait must leave workers intact until checkpoint writes stop.
            done, _ = await asyncio.wait(self._inflight, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                if not task.cancelled():
                    await task
        self.error_handler.check()

    async def abort(self) -> None:
        """Cancel consumers before awaiting cleanup; no retries or final processing."""
        tasks = [*self._pump_tasks, *self._inflight]
        for task in tasks:
            task.cancel()
        try:
            await self._page.abort()
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
            self._pump_tasks.clear()

    def summary(self) -> dict[str, Any]:
        return {
            **summary(self.run_state, tokens_used=self._budget.used),
            "state": self._task.state if self._task else "CREATED",
            "reason": self._task.stopping_reason if self._task else None,
            "usage": self._budget.dump(),
            "dedup": self._dedup.report,
        }

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._reconcile()
            for close in (self.abort, self._dedup.aclose, self._ranking.aclose, self._page.aclose):
                try:
                    await close()
                except Exception as exc:
                    self._fail("cleanup", exc)
            if self._task is not None and self.failure:
                self._task.state = "FAILED"
                self._storage.save_task(self._task.model_dump(mode="json"))
        finally:
            try:
                await self._storage.close()
            finally:
                if self._own_lock and self._lock is not None:
                    self._lock.release()

    def _reconcile(self) -> None:
        """Report fetched, pending and refused work alongside the stop reason."""
        logger.info("crawl totals: %d pages and %d tokens", self.run_state.progress.pages_fetched, self._budget.used)
        left_frontier = self._frontier.size
        left_buffer = self._frontier.waiting_size
        stats = self.run_state.stats
        logger.debug(
            "task.reconcile discovered=%d ranked=%d fetched=%d left_in_frontier=%d left_in_buffer=%d",
            stats.links_discovered,
            stats.candidates_ranked,
            self.run_state.progress.pages_fetched,
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

    @activity("seed expander")
    async def expand_seeds(self, goal: CrawlGoal, seeds: list[Candidate]) -> list[Candidate]:
        """Propose and verify additional seeds when enabled."""
        expander = self._seed_expander
        if expander is None or not seeds:
            return []
        want = how_many(len(seeds), self._cfg.expand_seeds_min, self._cfg.expand_seeds_max)
        proposals = await self.error_handler.call(
            "seeds",
            lambda: expander.propose(goal, [c.url.raw for c in seeds], want),
            optional=True,
            repeatable=True,
        )
        if isinstance(proposals, Skipped):
            return []
        if not proposals:
            self.run_state.seeds_asked = 0
            self.run_state.rejected_seeds = []
            return []
        logger.info("the model named %d more source%s to try", len(proposals), "" if len(proposals) == 1 else "s")
        proposed, rejected = await verify(
            proposals,
            probe=self._page.probe,
            canonicalizer=self._canonicalizer,
        )
        logger.info("%d of the %d it named answered", len(proposed), len(proposals))
        self.run_state.seeds_asked = len(proposals)
        self.run_state.rejected_seeds = rejected
        for c in proposed:
            # url_key, the shape the page book counts under.
            key = self._canonicalizer.canonicalize(c.url.raw, c.url.raw).url_key
            self.run_state.proposed_seeds[key] = (c.url.canonical, str(c.signals.get("why", "")))
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
            self.run_state.seeds[url.url_key].url = url.canonical
            n_ingested += 1
        if items:
            await self._frontier.push_batch(items)
        if self._events and n_ingested > 0:
            self._events.emit(EventType.URL_DISCOVERED, {"source": "seed", "count": n_ingested})
        logger.info("starting from %d seed%s", n_ingested, "" if n_ingested == 1 else "s")
        return n_ingested

    async def _fetch_pump(self) -> None:
        while True:
            if not self._context.can_start("dispatch"):
                await asyncio.sleep(_POP_SLEEP)
                continue

            # Bound dispatched tasks independently of the fetch and analysis semaphores.
            if self.run_state.progress.in_flight >= self._cfg.fetch_concurrency + 2 * self._cfg.llm_concurrency:
                await asyncio.sleep(_POP_SLEEP)
                continue

            item = await self._frontier.pop_next(
                now=_utcnow(),
                next_allowed=None if self._cfg.ignore_robots else self._robots.next_allowed_at,
                global_budget=self.run_state.limits.max_pages,
            )
            if item is None:
                if self._frontier.scoring > 0:
                    # A running rank call still holds pending work; wait for it to return.
                    await asyncio.sleep(_POP_SLEEP)
                    continue
                if self._frontier.waiting_size == 0:
                    # Wake ranking to observe state changes while fetching or cooldowns continue.
                    await self._frontier.wake_ranker()
                elif self._frontier.size == 0 and self.run_state.progress.in_flight == 0:
                    # Wake ranking when buffered work remains but fetching has no candidates.
                    logger.debug(
                        "fetch_pump.waking_rank frontier=%d buffer=%d", self._frontier.size, self._frontier.waiting_size
                    )
                    await self._frontier.wake_ranker()
                await asyncio.sleep(_POP_SLEEP)
                continue

            self._dispatch_page(item)

    def _dispatch_page(self, item: FrontierItem) -> None:
        self.run_state.work.setdefault(item.item_id, {"item": item.model_dump(mode="json")})
        self.run_state.work_started()
        task = asyncio.create_task(self._run_page(item))
        self._inflight.add(task)
        task.add_done_callback(self._page_done)

    async def _run_page(self, item: FrontierItem) -> None:
        assert self._goal is not None
        try:
            await self._page.run(PageInput(item, self._goal, self._task_id, self._events), self._context)
        finally:
            self.run_state.work_finished()

    async def _rank_pump(self) -> None:
        while True:
            logger.debug("rank_pump.wait frontier=%d buffer=%d", self._frontier.size, self._frontier.waiting_size)
            await self._frontier.wait_for_ranking(lambda: not self._context.can_start("run"))
            logger.debug(
                "rank_pump.woke frontier=%d buffer=%d state=%s",
                self._frontier.size,
                self._frontier.waiting_size,
                self._context.can_start("run"),
            )
            if not self._context.can_start("run"):
                await asyncio.sleep(_POP_SLEEP)
                continue

            batch = await self._frontier.take_for_ranking(_RANK_BATCH_SIZE)
            if not batch:
                continue

            assert self._goal is not None
            logger.debug("rank_pump.drain batch=%d frontier=%d", len(batch), self._frontier.size)
            try:
                await self._ranking.run(batch, self._goal, self._events)
            except BaseException:
                await self._frontier.return_for_ranking(batch)
                raise
            finally:
                self._frontier.finish_ranking(batch)
            await self._context.checkpoint()
