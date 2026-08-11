"""CrawlScheduler — the orchestrator that wires all V0.1 modules together.

Drives two concurrent asyncio tasks (fetch_pump + rank_pump), runs stop-condition
checks each iteration, handles pause / resume / stop, and saves periodic
checkpoints for crash recovery.

V0.1 path (no LLM):
  - Page Analyzer is skipped (v0.2)
  - FeedbackStore is empty (v0.2)
  - tokens_used always 0
  - HybridRanker uses RuleScorer only

See docs/arch.md fetch_pump / rank_pump for the pseudocode this follows.
"""

from __future__ import annotations

import asyncio
import datetime
import time
from typing import Any

from crawlme.config import Settings
from crawlme.digest.extractor import Extractor
from crawlme.digest.fetcher import Fetcher
from crawlme.digest.links import extract_links
from crawlme.pioneer.buffer import CandidateBuffer
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.pioneer.frontier import Frontier
from crawlme.pioneer.prefilter import PreFilter, PreFilterContext
from crawlme.pioneer.ranker import HybridRanker
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.scheduler.stop_conds import check_stop
from crawlme.schemas import (
    URL,
    Candidate,
    CrawlGoal,
    CrawlTask,
    FrontierItem,
    FrontierSnapshot,
    RankHistorySummary,
)
from crawlme.state.storage import Storage

_CHECKPOINT_INTERVAL = 10
_RANK_BATCH_SIZE = 100
_POP_SLEEP = 0.2


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class CrawlScheduler:
    def __init__(
        self,
        settings: Settings | None = None,
        storage: Storage | None = None,
        frontier: Frontier | None = None,
        fetcher: Fetcher | None = None,
        extractor: Extractor | None = None,
        robots: RobotsPolicy | None = None,
        prefilter: PreFilter | None = None,
        buffer: CandidateBuffer | None = None,
        ranker: HybridRanker | None = None,
        canonicalizer: Canonicalizer | None = None,
    ) -> None:
        cfg = settings or Settings()
        self._cfg = cfg
        self._storage = storage or Storage(str(cfg.db_path), str(cfg.raw_dir))
        self._frontier = frontier or Frontier(domain_budget=cfg.default_domain_budget)
        self._fetcher = fetcher or Fetcher(
            user_agents=list(cfg.user_agents),
            connect_timeout=cfg.fetch_timeout_connect,
            read_timeout=cfg.fetch_timeout_read,
            max_retries=cfg.fetch_max_retries,
        )
        self._extractor = extractor or Extractor()
        self._robots = robots or RobotsPolicy(ignore=cfg.ignore_robots)
        self._prefilter = prefilter or PreFilter()
        self._buffer = buffer or CandidateBuffer(capacity=cfg.candidate_buffer_size)
        self._ranker = ranker or HybridRanker()
        self._canonicalizer = canonicalizer or Canonicalizer()

        self._fetch_sem = asyncio.Semaphore(cfg.fetch_concurrency)
        self._llm_sem = asyncio.Semaphore(cfg.llm_concurrency)

        self._state: str = "CREATED"
        self._counters: dict[str, Any] = {}
        self._goal: CrawlGoal | None = None
        self._task: CrawlTask | None = None
        self._pump_tasks: list[asyncio.Task[None]] = []
        self._inflight_tasks: set[asyncio.Task[None]] = set()

    # -- public API -------------------------------------------------------

    async def run(self, goal: CrawlGoal, task: CrawlTask) -> None:
        self._goal = goal
        self._task = task
        self._state = "RUNNING"
        task.state = "RUNNING"

        await self._storage.start()

        self._counters = {
            "max_pages": goal.max_pages,
            "max_tokens": goal.max_tokens,
            "max_duration_sec": goal.max_duration_sec,
            "min_relevant_hits": goal.min_relevant_hits,
            "relevance_threshold": goal.relevance_threshold,
            "pages_fetched": 0,
            "tokens_used": 0,
            "started_at": time.monotonic(),
            "in_flight": 0,
            "relevance_window": [],
            "fatal_error": "",
        }

        self._pump_tasks = [
            asyncio.create_task(self._fetch_pump()),
            asyncio.create_task(self._rank_pump()),
        ]
        await asyncio.gather(*self._pump_tasks, return_exceptions=True)

        task.state = "COMPLETED"
        task.end_at = _utcnow()
        await self._storage.close()

    async def pause(self) -> None:
        self._state = "PAUSING"
        # Wait for in-flight fetches to finish.
        while self._counters.get("in_flight", 0) > 0:
            await asyncio.sleep(0.1)
        self._state = "PAUSED"
        if self._task:
            self._task.state = "PAUSED"
            await self._checkpoint()

    async def resume(self) -> None:
        if self._state != "PAUSED":
            return
        # Restore from latest checkpoint.
        snap = await self._load_latest_snapshot()
        if snap:
            self._frontier.restore(snap)
        self._state = "RUNNING"
        if self._task:
            self._task.state = "RUNNING"
        self._counters["started_at"] = time.monotonic()
        self._pump_tasks = [
            asyncio.create_task(self._fetch_pump()),
            asyncio.create_task(self._rank_pump()),
        ]
        await asyncio.gather(*self._pump_tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._state = "STOPPING"
        if self._task:
            self._task.state = "STOPPING"

    # -- fetch loop -------------------------------------------------------

    async def _fetch_pump(self) -> None:
        while self._state == "RUNNING":
            reasons = check_stop(
                self._task,  # type: ignore[arg-type]
                self._frontier,
                self._buffer,
                self._counters,
            )
            if reasons:
                self._task.stopping_reason = "+".join(r.code for r in reasons)  # type: ignore[union-attr]
                self._state = "STOPPING"
                break

            item = await self._frontier.pop_next(
                now=_utcnow(),
                next_allowed=self._robots.next_allowed_at,
                global_budget=self._counters.get("max_pages"),
            )
            if item is None:
                if self._buffer.is_empty and self._counters.get("in_flight", 0) == 0:
                    break
                await asyncio.sleep(_POP_SLEEP)
                continue

            self._counters["in_flight"] = self._counters.get("in_flight", 0) + 1
            self._inflight_tasks.add(asyncio.create_task(self._handle_fetch(item)))

        self._state = "STOPPING"

    async def _handle_fetch(self, item: FrontierItem) -> None:
        async with self._fetch_sem:
            try:
                raw_path = ""
                try:
                    result = await self._fetcher.fetch(item)
                    domain = item.url.reg_domain or _extract_domain(item.url.raw)
                    self._robots.record_response(domain, result.status_code)
                except Exception:
                    await self._frontier.record_outcome(item, "FAILED")
                    return

                # Extract content.
                raw_path = self._storage.raw_html_path(item.url_key, result.item_id)
                self._storage.save_raw_html(item.url_key, result.item_id, result.raw)
                page = self._extractor.extract(result, raw_path)
                self._storage.save_page(_page_to_json(page))

                # Extract links → Candidates → PreFilter → Buffer.
                raw_links = extract_links(page)
                ctx = PreFilterContext(
                    visited=self._frontier._visited,
                    frontier_keys=set(self._frontier._items.keys()),
                    domain_counters=self._frontier._domain_counters,
                    allow_fetch=lambda url: self._robots.allow_fetch(url),
                )
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
                    decision, _rule_name = self._prefilter.check(c, self._goal, ctx)  # type: ignore[arg-type]
                    if decision.value == "allow":
                        c.status = "BUFFERED"
                        await self._buffer.add([c])
                    else:
                        c.status = "FILTERED_OUT"
                    # Persist candidate for audit trail.
                    self._storage.save_candidate(
                        {
                            "candidate_id": c.candidate_id,
                            "url_key": c.url.url_key,
                            "url_json": c.url.model_dump(),
                            "anchor": c.anchor,
                            "snippet": c.snippet,
                            "parent_heading": c.parent_heading,
                            "position": c.position,
                            "source_page_id": c.source_page_id,
                            "source_url_key": c.source_url_key,
                            "depth": c.depth,
                            "status": c.status,
                            "discovered_at": c.discovered_at.isoformat() if c.discovered_at else "",
                        }
                    )

                await self._frontier.record_outcome(item, "COMPLETED")

                self._counters["pages_fetched"] = self._counters.get("pages_fetched", 0) + 1

                # Periodic checkpoint.
                if self._counters["pages_fetched"] % _CHECKPOINT_INTERVAL == 0:
                    await self._checkpoint()

            finally:
                self._counters["in_flight"] = max(0, self._counters.get("in_flight", 0) - 1)
                task = asyncio.current_task()
                if task:
                    self._inflight_tasks.discard(task)

    # -- rank loop --------------------------------------------------------

    async def _rank_pump(self) -> None:
        while self._state == "RUNNING":
            await self._buffer.wait_until(lambda: self._buffer.ready(self._frontier.size == 0))
            if self._state != "RUNNING":
                break

            batch = await self._buffer.drain(_RANK_BATCH_SIZE)
            if not batch:
                continue

            history = RankHistorySummary(pages_seen=self._counters.get("pages_fetched", 0))
            assert self._goal is not None
            decisions = await self._ranker.rank_batch(self._goal, batch, history)

            items: list[FrontierItem] = []
            for d in decisions:
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

            # In V0.1, tokens_used is always 0 (no LLM calls).
            self._counters["tokens_used"] = self._counters.get("tokens_used", 0)

    # -- checkpoint -------------------------------------------------------

    async def _checkpoint(self) -> None:
        if self._task is None:
            return
        snap = self._frontier.snapshot(task_id=self._task.task_id)
        snap_id = f"{self._task.task_id}-latest"
        self._storage.save_snapshot(
            {
                "snapshot_id": snap_id,
                "task_id": snap.task_id,
                "snapshot_json": snap.model_dump(),
                "created_at": _utcnow().isoformat(),
            }
        )

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
        return FrontierSnapshot(**snap_json)


# -- helpers -------------------------------------------------------------


def _extract_domain(raw_url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(raw_url).hostname or "").lower()


def _find_candidate(batch: list[Candidate], candidate_id: str) -> Candidate | None:
    for c in batch:
        if c.candidate_id == candidate_id:
            return c
    return None


def _page_to_json(page: Any) -> dict[str, Any]:
    return {
        "page_id": page.page_id,
        "url_key": page.url_key,
        "url_json": page.url.model_dump() if hasattr(page.url, "model_dump") else {},
        "raw_html_path": page.raw_html_path,
        "title": page.title,
        "markdown": page.markdown,
        "plain_text": page.plain_text,
        "metadata_json": page.metadata,
        "text_hash": page.text_hash,
        "text_len": page.text_len,
        "extracted_at": page.extracted_at.isoformat() if page.extracted_at else "",
        "extraction_status": page.extraction_status,
    }
