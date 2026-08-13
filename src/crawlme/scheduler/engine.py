"""CrawlScheduler: the orchestrator that wires all v0.1 modules together.

Drives two concurrent asyncio tasks (fetch_pump + rank_pump), runs stop-condition
checks each iteration, handles pause / resume / stop, and saves periodic
checkpoints for crash recovery.

v0.1 path (no LLM):
  - Page Analyzer is skipped (v0.2)
  - FeedbackStore is empty (v0.2)
  - tokens_used always 0
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
    Candidate,
    CrawlCounters,
    CrawlGoal,
    CrawlTask,
    FrontierItem,
    FrontierSnapshot,
    RankHistorySummary,
)
from crawlme.state.events import EventEmitter, EventType
from crawlme.state.storage import Storage

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
        storage: Storage,
        frontier: Frontier,
        fetcher: Fetcher,
        extractor: Extractor,
        robots: RobotsPolicy,
        prefilter: PreFilter,
        buffer: Buffer,
        ranker: Ranker,
        canonicalizer: Canonicalizer,
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

        self._fetch_sem = asyncio.Semaphore(settings.fetch_concurrency)
        self._llm_sem = asyncio.Semaphore(settings.llm_concurrency)

        self._state: str = "CREATED"
        self._counters: CrawlCounters = CrawlCounters()
        self._goal: CrawlGoal | None = None
        self._task: CrawlTask | None = None
        self._pump_tasks: list[asyncio.Task[None]] = []
        # Maps url_key -> {title, link_count} so the ranker can use per-page
        # signals (title_match F3 + position F7) instead of defaulting to 0.5.
        self._page_contexts: dict[str, dict[str, Any]] = {}
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

        self._counters = CrawlCounters(
            max_pages=goal.max_pages,
            max_tokens=goal.max_tokens,
            max_duration_sec=goal.max_duration_sec,
            min_relevant_hits=goal.min_relevant_hits,
            relevance_threshold=goal.relevance_threshold,
            started_at=time.monotonic(),
        )

        self._pump_tasks = [
            asyncio.create_task(self._fetch_pump()),
            asyncio.create_task(self._rank_pump()),
        ]
        await asyncio.gather(*self._pump_tasks, return_exceptions=True)

        task.state = "COMPLETED"
        task.end_at = _utcnow()
        reason = task.stopping_reason or "none"
        logger.info(
            "task.done task_id=%s pages=%d reason=%s",
            task.task_id,
            self._counters.pages_fetched,
            reason,
        )
        if self._events:
            self._events.emit(
                EventType.STOPPED,
                {"reason": reason, "pages_fetched": self._counters.pages_fetched},
            )
        await self._storage.close()

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

    async def _handle_fetch(self, item: FrontierItem) -> None:
        if self._events:
            self._events.emit(EventType.FETCH_STARTED, {"url_key": item.url_key, "depth": item.depth})
        async with self._fetch_sem:
            try:
                raw_path = ""
                try:
                    result = await self._fetcher.fetch(item)
                    domain = item.url.reg_domain or _extract_domain(item.url.raw)
                    self._robots.record_response(domain, result.status_code)
                except Exception:
                    logger.warning(
                        "fetch.failed url_key=%s domain=%s depth=%d", item.url_key, item.reg_domain, item.depth
                    )
                    if self._events:
                        self._events.emit(EventType.FETCH_FAILED, {"url_key": item.url_key, "depth": item.depth})
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
                raw_links = await asyncio.to_thread(extract_links, page)
                logger.debug(
                    "extracted url_key=%s title=%r links=%d status=%s",
                    page.url_key,
                    page.title,
                    len(raw_links),
                    page.extraction_status,
                )

                # Record page context for ranker (F3 title_match + F7 position).
                self._page_contexts[page.url_key] = {
                    "title": page.title or "",
                    "link_count": len(raw_links),
                }
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
                        self._storage.save_candidate(c)
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

            history = RankHistorySummary(pages_seen=self._counters.pages_fetched)
            assert self._goal is not None
            decisions = await self._ranker.rank_batch(self._goal, batch, history, page_contexts=self._page_contexts)

            n_dropped = sum(1 for d in decisions if d.dropped)
            n_kept = len(decisions) - n_dropped
            ranked_total += len(batch)
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
                        priority=d.priority,
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
