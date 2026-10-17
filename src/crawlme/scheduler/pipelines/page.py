"""Process a page through extraction, analysis, discovery and candidate admission."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from dataclasses import dataclass

from crawlme.config import Settings
from crawlme.digest import Extractor, FetchResult, Page
from crawlme.digest.fetcher.base import FetchError
from crawlme.discovery import Harvest
from crawlme.errors import Kind
from crawlme.persist import Storage
from crawlme.pioneer import Candidate, FrontierItem
from crawlme.pioneer.frontier import Frontier
from crawlme.pioneer.prefilter import PreFilter, PreFilterContext, is_stale
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.platforms.base import FeedDependencyError
from crawlme.runtime import CrawlGoal, EventEmitter, EventType, RunState, Tracker, why_retire
from crawlme.scheduler.pipelines import Pipeline, PipelineContext
from crawlme.scheduler.workers import AnalysisWorker, DiscoveryWorker, FetchFailure, FetchWorker, PersistWorker
from crawlme.urls import Canonicalizer

logger = logging.getLogger(__name__)

_MAX_LISTING_PAGES = 5
_LISTING_PAGE_PRIORITY = 0.5


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass(frozen=True)
class FetchedPage:
    result: FetchResult
    page: Page


@dataclass(frozen=True)
class PageInput:
    item: FrontierItem
    goal: CrawlGoal


class PagePipeline(Pipeline[PageInput, None]):
    def __init__(
        self,
        *,
        settings: Settings,
        storage: Storage,
        frontier: Frontier,
        fetch: FetchWorker,
        persist: PersistWorker,
        extractor: Extractor,
        analysis: AnalysisWorker,
        discovery: DiscoveryWorker,
        robots: RobotsPolicy,
        prefilter: PreFilter,
        canonicalizer: Canonicalizer,
        tracking: Tracker,
    ) -> None:
        self._cfg = settings
        self._storage = storage
        self._frontier = frontier
        self._fetch = fetch
        self._persist = persist
        self._extractor = extractor
        self._analysis = analysis
        self._discovery = discovery
        self._robots = robots
        self._prefilter = prefilter
        self._canonicalizer = canonicalizer
        self._tracking = tracking
        self._active: dict[str, set[str]] = {}
        self._started = False
        analysis.bind_completion(self.analysis_completed)
        self._page_slots = asyncio.Semaphore(settings.fetch_concurrency)

    @property
    def run_state(self) -> RunState:
        return self._tracking.state

    def prepare(self, item: FrontierItem) -> None:
        self.run_state.work.setdefault(item.item_id, {"item": item.model_dump(mode="json")})
        self.retain_page(item)

    async def aclose(self) -> None:
        await self._fetch.aclose()

    async def probe(self, item: FrontierItem) -> Harvest:
        # Verification precedes run(), but the shared robots cache already needs storage.
        await self._storage.start()
        result = await self._fetch.fetch(item)
        if isinstance(result, FetchFailure):
            if result.error is not None:
                raise result.error
            raise FetchError(
                "blocked by robots" if result.reason == "robots" else "could not be fetched",
                code="robots_disallowed" if result.reason == "robots" else "fetch_failed",
                kind=Kind.FORBIDDEN if result.reason == "robots" else Kind.UNAVAILABLE,
                details={"url_key": item.url_key},
            )
        page = Page(
            url_key=item.url_key,
            url=item.url,
            raw_html_path=await self._persist.save_raw(item.url_key, result),
            payload_paths=await self._persist.save_payloads(item.url_key, result),
        )
        return await self._discovery.discover(page, item.depth)

    async def enqueue_next_page(
        self,
        next_url: str,
        item: FrontierItem,
        ctx: PreFilterContext,
        seed: str,
        goal: CrawlGoal,
    ) -> None:
        """Queue a capped listing continuation at the same depth and neutral priority."""
        pages = self.run_state.seeds[seed].listing_pages
        if pages >= _MAX_LISTING_PAGES:
            logger.debug("listing.page_cap seed=%s pages=%d", seed, pages)
            return
        url = self._canonicalizer.canonicalize(next_url, item.url.canonical)
        candidate = Candidate(url=url, depth=item.depth, discovered_at=_utcnow())
        decision, why = self._prefilter.check(candidate, goal, ctx)
        if decision.value != "allow":
            logger.debug("listing.next_dropped url=%s reason=%s", url.canonical, why)
            return
        self.run_state.seeds[seed].listing_pages = pages + 1
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

    async def fetch_and_extract(
        self,
        item: FrontierItem,
        *,
        task_id: str = "",
        events: EventEmitter | None = None,
    ) -> FetchedPage | None:
        async with self._page_slots:
            return await self._process_page(item, task_id, events)

    async def _process_page(
        self,
        item: FrontierItem,
        task_id: str,
        events: EventEmitter | None,
    ) -> FetchedPage | None:
        outcome = await self._fetch.fetch(item)
        if isinstance(outcome, FetchFailure):
            if outcome.reason == "robots":
                self.run_state.stats.robots_blocked += 1
            else:
                self.run_state.stats.fetch_errors += 1
                if events:
                    events.emit(EventType.FETCH_FAILED, {"url_key": item.url_key, "depth": item.depth})
                self._storage.save_error(
                    {
                        "task_id": task_id,
                        "url_key": item.url_key,
                        "stage": "fetch",
                        "error_type": outcome.error_type,
                        "attempt": item.attempts,
                        "created_at": _utcnow().isoformat(),
                    }
                )
            await self._frontier.record_outcome(item, "FAILED" if outcome.reason == "fetch" else "SKIPPED")
            return None
        raw_path = await self._persist.save_raw(item.url_key, outcome)
        logger.debug("fetch.extracting url_key=%s size=%dKB", item.url_key, len(outcome.raw) // 1024)
        try:
            page = await asyncio.wait_for(
                asyncio.to_thread(self._extractor.extract, outcome, raw_path), timeout=self._cfg.extract_timeout
            )
        except asyncio.TimeoutError:
            logger.warning("fetch.extract_timeout url_key=%s size=%dKB", item.url_key, len(outcome.raw) // 1024)
            self.run_state.page_completed()
            await self._frontier.record_outcome(item, "SKIPPED")
            return None
        await self._persist.save_extracted(item.url_key, outcome, page)
        self.published(page, self.run_state.pages.seed_of(item.url_key, item.seed_url_key or item.url_key))
        return FetchedPage(outcome, page)

    async def run(self, input: PageInput, context: PipelineContext) -> None:
        item, goal = input.item, input.goal
        task_id, events = context.task_id, context.events
        work = self.run_state.work.setdefault(item.item_id, {"item": item.model_dump(mode="json")})
        keys = self.retain_page(item)
        if events:
            events.emit(EventType.FETCH_STARTED, {"url_key": item.url_key, "depth": item.depth})
        try:
            if "page" in work:
                page = Page.model_validate(work["page"])
            else:
                fetched = await self.fetch_and_extract(item, task_id=task_id, events=events)
                if fetched is None:
                    self.run_state.work.pop(item.item_id, None)
                    return
                page = fetched.page
                work["page"] = page.model_dump(mode="json")
                work["status"] = fetched.result.status_code
                work["size"] = len(fetched.result.raw)
                await context.checkpoint()
            keys.add(page.url_key)

            self.run_state.pages.open(
                page.url_key,
                page.url.canonical,
                self.run_state.pages.seed_of(item.url_key, item.seed_url_key or item.url_key),
            )
            if not work.get("analysis_done"):
                if is_stale(page.published_at, goal.since):
                    logger.debug("analysis.skipped url_key=%s reason=stale", page.url_key)
                else:
                    await self._analysis.analyze(page, goal, allowed=context.can_analyze)
                work["analysis_done"] = True
                await context.checkpoint()
            if events:
                events.emit(
                    EventType.FETCH_COMPLETED,
                    {"url_key": item.url_key, "status": work["status"], "size": work["size"]},
                )
                events.emit(
                    EventType.PAGE_EXTRACTED,
                    {"url_key": page.url_key, "title": page.title, "status": page.extraction_status},
                )

            try:
                harvest = await self._discovery.discover(page, item.depth)
            except FeedDependencyError as e:
                # Missing format dependencies stop the run rather than producing empty listings.
                logger.error("fetch.adapter_dependency url_key=%s: %s", item.url_key, e)
                raise
            if not work.get("discovered"):
                self.discovered(page, item, harvest)
                work["discovered"] = True
            else:
                for candidate in harvest.candidates:
                    candidate.seed_url_key = item.seed_url_key or item.url_key
                    candidate.seed_ext = item.seed_ext
            candidates = harvest.candidates
            seed = self.run_state.pages.seed_of(item.url_key, item.seed_url_key or item.url_key)
            ctx = self._frontier.get_prefilter_context(
                allow_fetch=lambda url: self._robots.allow_fetch(url),
            )
            n_allowed = 0
            n_filtered = 0
            for c in candidates:
                decision, _ = self._prefilter.check(c, goal, ctx)
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
            if harvest.next_url and not work.get("next_page"):
                await self.enqueue_next_page(harvest.next_url, item, ctx, seed, goal)
                work["next_page"] = True
            if events and n_allowed > 0:
                events.emit(
                    EventType.URL_DISCOVERED,
                    {"source_url_key": page.url_key, "count": n_allowed, "filtered": n_filtered},
                )

            await self._frontier.record_outcome(item, "COMPLETED")

            self.run_state.page_completed()
            n = self.run_state.progress.pages_fetched
            logger.debug(
                "fetch.ok #%d url_key=%s title=%r links=%d allowed=%d elapsed=%.1fs",
                n,
                page.url_key,
                page.title,
                len(candidates),
                n_allowed,
                (time.monotonic() - self.run_state.progress.started_at),
            )

            self.run_state.work.pop(item.item_id, None)
            self.release_page(item.item_id)
            self.prune()
            await context.checkpoint()

        except asyncio.CancelledError:
            raise
        except BaseException:
            await self._frontier.record_outcome(item, "FAILED")
            raise
        finally:
            self.release_page(item.item_id)
            self.prune()

    def start(self) -> None:
        self._started = True

    def analysis_completed(self, seed: str | None) -> None:
        self.retire(seed)
        self.prune()

    def retain_page(self, item: FrontierItem) -> set[str]:
        keys = self._active.setdefault(item.item_id, {item.url_key})
        keys.add(self.run_state.work.get(item.item_id, {}).get("page", {}).get("url_key", item.url_key))
        return keys

    def release_page(self, item_id: str) -> None:
        self._active.pop(item_id, None)

    def prune(self) -> None:
        if self._started:
            active = set().union(*self._active.values())
            self._tracking.retain(active | self._analysis.pending_keys | self._frontier.context_keys())

    def discovered(self, page: Page, item: FrontierItem, harvest: Harvest) -> None:
        self.retire(self._tracking.discovered(page, item, harvest))

    def published(self, page: Page, seed: str) -> None:
        self.retire(self._tracking.published(page, seed))

    def vote(self, url_key: str) -> None:
        self.retire(self._tracking.vote(url_key))

    def retire(self, seed: str | None) -> None:
        if seed is None:
            return
        state = self.run_state.seeds[seed]
        why = why_retire(state.window, state.stale)
        if why is not None and self._tracking.retire(seed, why):
            self._frontier.retire(seed)
            logger.info("done with one source: %s", why)
