"""Verify page processing through its owning component."""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.analysis import AnalysisResult, AnalyzerFeedback
from crawlme.config import Settings
from crawlme.digest import FetchError, FetchResult, Page, Payload
from crawlme.discovery import Harvest
from crawlme.errors import Error, Kind
from crawlme.pioneer import FrontierItem
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.scheduler.factory import create_scheduler
from crawlme.scheduler.pipelines.page import FetchedPage
from crawlme.urls import URL
from tests.scheduler.helpers import (
    _goal,
    _item,
    _make_sched,
    _paging_sched,
    _robots_sched,
    _set_context,
    _task,
    _url,
    _utcnow,
)


def _worker(tmp_path, *, extract=None, timeout=1):
    url = URL(raw="https://example.com/p", canonical="https://example.com/p", url_key="p", reg_domain="example.com")
    item = FrontierItem(url=url, url_key="p", reg_domain="example.com")
    result = FetchResult(
        item_id=item.item_id,
        url=url,
        url_key="p",
        status_code=200,
        raw=b"<p>body</p>",
        payloads=[Payload(url="https://example.com/api", body=b"{}")],
    )
    fetcher = MagicMock(fetch=AsyncMock(return_value=result), aclose=AsyncMock())
    storage = MagicMock()
    storage.raw_html_path.return_value = str(tmp_path / "page.html")
    storage.save_payload.return_value = str(tmp_path / "payload.json")
    page = Page(url=url, url_key="p")
    extractor = MagicMock(extract=extract or MagicMock(return_value=page))
    frontier = MagicMock(record_outcome=AsyncMock())
    worker = create_scheduler(
        Settings(result_dir=tmp_path, fetch_concurrency=1, extract_timeout=timeout, analysis_enabled=False),
        fetcher=fetcher,
        extractor=extractor,
        robots=RobotsPolicy(ignore=True),
        storage=storage,
        frontier=frontier,
    )
    return worker.pipeline._page, item, storage


@pytest.mark.asyncio
async def test_persist_before_return(tmp_path):
    worker, item, storage = _worker(tmp_path)

    def extract(result, path):
        storage.save_raw_html.assert_called_once_with("p", item.item_id, result.raw)
        storage.save_payload.assert_not_called()
        storage.save_page.assert_not_called()
        assert path == str(tmp_path / "page.html")
        return Page(url=result.url, url_key=result.url_key, raw_html_path=path)

    worker._extractor.extract.side_effect = extract
    outcome = await worker.fetch_and_extract(item)
    assert isinstance(outcome, FetchedPage)
    assert outcome.page.payload_paths == [str(tmp_path / "payload.json")]
    storage.save_raw_html.assert_called_once_with("p", item.item_id, b"<p>body</p>")
    storage.save_page.assert_called_once_with(outcome.page)
    assert not worker._page_slots.locked()


@pytest.mark.asyncio
async def test_raw_write_failure_stops_extraction(tmp_path):
    worker, item, storage = _worker(tmp_path)
    storage.save_raw_html.side_effect = OSError("disk full")
    with pytest.raises(OSError, match="disk full"):
        await worker.fetch_and_extract(item)
    worker._extractor.extract.assert_not_called()
    storage.save_payload.assert_not_called()
    storage.save_page.assert_not_called()
    assert not worker._page_slots.locked()


@pytest.mark.asyncio
async def test_page_slot_covers_persistence(tmp_path):
    pipeline, item, _ = _worker(tmp_path)
    saving = asyncio.Event()
    release = asyncio.Event()
    second_started = asyncio.Event()

    async def save_raw(*_):
        saving.set()
        await release.wait()
        return str(tmp_path / "page.html")

    pipeline._persist.save_raw = AsyncMock(side_effect=save_raw)

    async def second_page():
        second_started.set()
        return await pipeline.fetch_and_extract(item)

    first = asyncio.create_task(pipeline.fetch_and_extract(item))
    second = None
    try:
        await asyncio.wait_for(saving.wait(), 1)
        second = asyncio.create_task(second_page())
        await asyncio.wait_for(second_started.wait(), 1)
        pipeline._fetch.fetcher.fetch.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []))
    assert pipeline._fetch.fetcher.fetch.await_count == 2


@pytest.mark.asyncio
async def test_fetch_failure_result(tmp_path):
    worker, item, storage = _worker(tmp_path)
    worker._fetch.fetcher.fetch.side_effect = FetchError("offline", kind=Kind.NOT_FOUND)
    assert await worker.fetch_and_extract(item) is None
    assert worker.run_state.stats.fetch_errors == 1
    storage.save_page.assert_not_called()
    assert not worker._page_slots.locked()


@pytest.mark.asyncio
async def test_extract_timeout_result(tmp_path):
    release = threading.Event()
    finished = threading.Event()

    def extract(result, path):
        try:
            release.wait(timeout=2)
            return Page(url=result.url, url_key=result.url_key)
        finally:
            finished.set()

    worker, item, storage = _worker(tmp_path, extract=extract, timeout=0.02)
    try:
        assert await worker.fetch_and_extract(item) is None
        assert worker.run_state.progress.pages_fetched == 1
        worker._frontier.record_outcome.assert_awaited_once_with(item, "SKIPPED")
        storage.save_raw_html.assert_called_once()
        storage.save_payload.assert_not_called()
        storage.save_page.assert_not_called()
        assert not worker._page_slots.locked()
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 2)


@pytest.mark.asyncio
async def test_harvest_timeout(monkeypatch):
    """A page whose link extraction hangs must not stall the crawl.

    The page still counts as fetched (it was fetched, extracted, and
    analyzed); only its link harvest is lost.  Regression for the
    unbounded extract_links call that could freeze the fetch pump on a
    pathological page.
    """
    done = threading.Event()

    def _slow_links(_page, _depth):
        done.wait(10)  # released by the test so the worker thread exits
        return Harvest([])

    sched = _make_sched()
    sched.pipeline._goal = _goal(max_pages=5)
    sched._task = _task()
    sched._checkpoint = AsyncMock()
    _set_context(sched, max_pages=5, max_tokens=100000, max_duration_sec=3600)
    sched.pipeline._page._discovery._timeout = 0.2
    sched.pipeline._page._fetch.fetcher.fetch = AsyncMock(
        return_value=FetchResult(item_id="i1", url_key="k1", url=_item().url, raw=b"<html></html>")
    )
    sched.pipeline._page._extractor.extract = MagicMock(
        return_value=Page(
            url_key="k1",
            url=URL(raw="https://example.com", canonical="https://example.com", url_key="k1"),
            title="slow page",
        )
    )
    # The harvester is injected now, so a pathological page is
    # simulated by a slow harvest rather than a patched import.
    sched.pipeline._page._discovery.harvester = MagicMock(harvest=_slow_links)
    sched.pipeline._frontier.record_outcome = AsyncMock()

    try:
        await sched.pipeline._run_page(_item())
    finally:
        done.set()

    assert sched.run_state.progress.pages_fetched == 1
    args = sched.pipeline._frontier.record_outcome.call_args[0]
    assert args[1] == "COMPLETED"


@pytest.mark.asyncio
async def test_analysis_free(monkeypatch):
    """Waiting on the LLM must not occupy fetch concurrency.

    Regression: analyze used to run inside the fetch semaphore, which made
    fetch_concurrency and llm_concurrency nested instead of independent.
    """
    sched = _make_sched(settings=Settings(fetch_concurrency=1))
    sched.pipeline._page._discovery.harvester = MagicMock(harvest=lambda page, depth: Harvest([]))
    sched.pipeline._goal = _goal()
    sched._task = _task()

    url = URL(raw="https://x.com/a", canonical="https://x.com/a", url_key="k1")
    page = Page(url_key="k1", url=url)
    result = MagicMock(item_id="i1", status_code=200, raw=b"x")

    held: dict[str, bool] = {}

    async def _analyze(_page, _goal_arg):
        held["locked"] = sched.pipeline._page._fetch._slots.locked()

    sched.pipeline._analysis.analyzer = MagicMock(analyze=AsyncMock(side_effect=_analyze))
    sched.pipeline._page.fetch_and_extract = AsyncMock(return_value=FetchedPage(result, page))
    sched.pipeline._frontier.record_outcome = AsyncMock()
    sched.pipeline._frontier.get_prefilter_context = MagicMock(return_value=MagicMock())
    sched._checkpoint = AsyncMock()

    await sched.pipeline._run_page(_item())

    assert held["locked"] is False


@pytest.mark.asyncio
async def test_slot_released():
    """The slot covers the request and its parse, nothing longer."""
    sched = _make_sched(settings=Settings(fetch_concurrency=1))
    sched.pipeline._page._fetch.fetcher.fetch = AsyncMock(side_effect=RuntimeError("boom"))
    sched.pipeline._frontier.record_outcome = AsyncMock()

    with pytest.raises(Error, match="boom"):
        await sched.pipeline._page.fetch_and_extract(_item())
    assert not sched.pipeline._page._fetch._slots.locked()


@pytest.mark.asyncio
async def test_missing_extra():
    """It is not about this page: every later page of the same format
    fails identically, so carrying on would spend the whole budget
    producing nothing and then report success."""
    from crawlme.platforms.base import FeedDependencyError

    sched = _make_sched()
    sched.pipeline._goal = _goal(max_pages=5)
    sched.pipeline._page._discovery.harvester = MagicMock(
        harvest=MagicMock(side_effect=FeedDependencyError("install crawl-me-maybe[rss]"))
    )
    url = URL(raw="https://x.com/a", canonical="https://x.com/a", url_key="k1")
    sched.pipeline._page.fetch_and_extract = AsyncMock(
        return_value=FetchedPage(MagicMock(item_id="i1", status_code=200, raw=b"x"), Page(url_key="k1", url=url))
    )
    sched.pipeline._frontier.record_outcome = AsyncMock()
    sched.pipeline._frontier.get_prefilter_context = MagicMock(return_value=MagicMock())
    sched._checkpoint = AsyncMock()

    with pytest.raises(FeedDependencyError, match="crawl-me-maybe"):
        await sched.pipeline._run_page(_item())


@pytest.mark.asyncio
async def test_page_same_depth():
    """More of the same listing, not a hop away from it. Counting it
    would spend the depth budget on standing still."""
    sched, frontier = _paging_sched()
    item = FrontierItem(url=_url("https://www.reddit.com/r/x/"), url_key="k", depth=2)
    ctx = MagicMock()
    await sched.pipeline._page.enqueue_next_page(
        "https://www.reddit.com/r/x/?after=t3_a", item, ctx, "seed", goal=sched.pipeline._goal
    )
    pushed = frontier.push_batch.await_args.args[0]
    assert [p.depth for p in pushed] == [2]
    assert pushed[0].score_source == "listing_page"


@pytest.mark.asyncio
async def test_page_not_top():
    """More raw material is worth less than a post the ranker liked. At
    full priority a six-page budget went entirely on listings."""
    sched, frontier = _paging_sched()
    item = FrontierItem(url=_url("https://www.reddit.com/r/x/"), url_key="k", depth=0)
    await sched.pipeline._page.enqueue_next_page(
        "https://www.reddit.com/r/x/?after=t3_a", item, MagicMock(), "seed", goal=sched.pipeline._goal
    )
    assert frontier.push_batch.await_args.args[0][0].priority < 1.0


@pytest.mark.asyncio
async def test_pages_capped():
    """A subreddit pages indefinitely, and its pages arrive at a seed's
    own depth, so nothing else would stop them."""
    from crawlme.scheduler.pipelines.page import _MAX_LISTING_PAGES

    sched, frontier = _paging_sched()
    item = FrontierItem(url=_url("https://www.reddit.com/r/x/"), url_key="k", depth=0)
    ctx = MagicMock()
    for i in range(_MAX_LISTING_PAGES + 3):
        await sched.pipeline._page.enqueue_next_page(
            f"https://www.reddit.com/r/x/?after=t3_{i}", item, ctx, "seed", goal=sched.pipeline._goal
        )
    assert frontier.push_batch.await_count == _MAX_LISTING_PAGES


@pytest.mark.asyncio
async def test_cap_per_listing():
    """Thirty accounts is thirty listings, not one budget between them."""
    from crawlme.scheduler.pipelines.page import _MAX_LISTING_PAGES

    sched, frontier = _paging_sched()
    ctx = MagicMock()
    for seed in ("a", "b"):
        item = FrontierItem(url=_url(f"https://www.reddit.com/r/{seed}/"), url_key=seed, depth=0)
        for i in range(_MAX_LISTING_PAGES):
            await sched.pipeline._page.enqueue_next_page(
                f"https://www.reddit.com/r/{seed}/?after=t3_{i}", item, ctx, seed, goal=sched.pipeline._goal
            )
    assert frontier.push_batch.await_count == _MAX_LISTING_PAGES * 2


@pytest.mark.asyncio
async def test_page_drop_uncap():
    """Robots or scope can refuse it, and a refusal must not eat the
    allowance for pages that would have been allowed."""
    sched, frontier = _paging_sched()
    sched.pipeline._prefilter.check.return_value = (MagicMock(value="drop"), "robots")
    item = FrontierItem(url=_url("https://www.reddit.com/r/x/"), url_key="k", depth=0)
    await sched.pipeline._page.enqueue_next_page(
        "https://www.reddit.com/r/x/?after=t3_a", item, MagicMock(), "seed", goal=sched.pipeline._goal
    )
    frontier.push_batch.assert_not_awaited()
    assert sched.run_state.seeds["seed"].listing_pages == 0


@pytest.mark.asyncio
async def test_robots_blocks():
    """robots.txt was fetched by nothing and enforced on nothing: the
    cache table was empty on every run and allow_fetch always said yes."""
    sched, _, fetcher = _robots_sched("User-agent: *\nDisallow: /\n")
    item = FrontierItem(url=_url("https://x.com/page"), url_key="k", reg_domain="x.com")
    assert await sched.pipeline._page.fetch_and_extract(item) is None
    # The only fetch was robots.txt itself.
    assert [c.args[0].url.canonical for c in fetcher.fetch.await_args_list] == ["https://x.com/robots.txt"]
    assert sched.run_state.stats.robots_blocked == 1


@pytest.mark.asyncio
async def test_robots_once():
    """Every page of a crawl on one host would otherwise pay for it."""
    sched, _, fetcher = _robots_sched("User-agent: *\nDisallow: /\n")
    for i in range(3):
        item = FrontierItem(url=_url(f"https://x.com/p{i}"), url_key=f"k{i}", reg_domain="x.com")
        await sched.pipeline._page.fetch_and_extract(item)
    robots_calls = [c for c in fetcher.fetch.await_args_list if c.args[0].url.canonical.endswith("robots.txt")]
    assert len(robots_calls) == 1


@pytest.mark.asyncio
async def test_robots_cached():
    """It survives the run it was read in, which is the point of a TTL."""
    cached = {"raw": "User-agent: *\nDisallow: /\n", "fetched_at": _utcnow().isoformat(), "ttl": 86400}
    sched, _, fetcher = _robots_sched("User-agent: *\nDisallow:\n", cached=cached)
    item = FrontierItem(url=_url("https://x.com/page"), url_key="k", reg_domain="x.com")
    assert await sched.pipeline._page.fetch_and_extract(item) is None
    fetcher.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_robots_absent():
    """A 404 states no policy, and a site briefly down must not have its
    whole domain closed off."""
    from crawlme.pioneer.robots import RobotsPolicy

    storage = MagicMock()
    storage.get_robots = AsyncMock(return_value=None)
    storage.flush = AsyncMock()
    fetcher = MagicMock(aclose=AsyncMock())
    fetcher.fetch = AsyncMock(side_effect=FetchError("connection refused", kind=Kind.NOT_FOUND))
    sched = _make_sched(storage=storage, fetcher=fetcher, robots=RobotsPolicy(agent="a"))
    sched.pipeline._goal = _goal(max_pages=5)
    await sched.pipeline._page._fetch.ensure_robots("x.com")
    assert sched.pipeline._robots.allow_fetch("https://x.com/anything")


@pytest.mark.asyncio
async def test_seed_credited_mid_judge():
    """The sink runs during the analyze call, and it reads maps that used
    to be filled only after that call returned. Every verdict therefore
    landed with its own page still unknown, so a proposed seed reported
    finding nothing however much it found."""
    sched = _make_sched()
    sched.pipeline._goal = _goal()
    sched.run_state.proposed_seeds = {"seed-k": ("https://ext.test/", "why")}
    sched.run_state.pages.open("k1", "https://ext.test/k1", "seed-k")

    url = URL(raw="https://ext.test/p/1", canonical="https://ext.test/p/1", url_key="pk", reg_domain="ext.test")
    page = Page(url_key="pk", url=url)
    result = MagicMock(item_id="i1", status_code=200, raw=b"x")

    async def _analyze(p, _g):
        sched.pipeline._analysis.apply(
            AnalysisResult(
                classification="RELEVANT",
                relevance_score=0.9,
                feedback=AnalyzerFeedback(url=p.url.canonical, title="t"),
            )
        )

    sched.pipeline._analysis.analyzer = MagicMock(analyze=AsyncMock(side_effect=_analyze))
    sched.pipeline._page.fetch_and_extract = AsyncMock(return_value=FetchedPage(result, page))
    sched.pipeline._frontier.record_outcome = AsyncMock()
    sched.pipeline._frontier.get_prefilter_context = MagicMock(return_value=MagicMock())
    sched._checkpoint = AsyncMock()

    await sched.pipeline._run_page(_item())

    why, tally = sched.summary()["proposed_seeds"]["https://ext.test/"]
    assert (why, tally[0]) == ("why", 1)


@pytest.mark.asyncio
async def test_a_met_target_stops_analysis():
    """The stop only stops dispatch. Every task already out keeps going
    and each one that lands adds to the tally, so one run asked for
    fifteen and reported twenty-four."""
    sched = _make_sched()
    sched.pipeline._goal = _goal()
    sched.run_state.limits = dataclasses.replace(sched.run_state.limits, max_relevant=15)
    sched.run_state.progress.relevant_found = 15
    sched.pipeline._analysis.analyzer = MagicMock(analyze=AsyncMock())
    url = URL(raw="https://x.test/p", canonical="https://x.test/p", url_key="pk", reg_domain="x.test")
    page = Page(url_key="pk", url=url)
    result = MagicMock(item_id="i", status_code=200, raw=b"x")
    sched.pipeline._page.fetch_and_extract = AsyncMock(return_value=FetchedPage(result, page))
    sched.pipeline._frontier.record_outcome = AsyncMock()
    sched.pipeline._frontier.get_prefilter_context = MagicMock(return_value=MagicMock())
    sched._checkpoint = AsyncMock()

    await sched.pipeline._run_page(_item())

    sched.pipeline._analysis.analyzer.analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_target_holds_under_a_queue():
    """The wait for an analysis slot used to happen inside the LLM
    client, past every check the scheduler could make, so a target met
    while forty-six pages were queued still had all forty-six analysed.
    Queued here instead, the check sits at the head of the queue and
    only what is already calling can overshoot."""
    sched = _make_sched(settings=Settings(llm_concurrency=2))
    sched.pipeline._goal = _goal()
    sched.run_state.limits = dataclasses.replace(sched.run_state.limits, max_relevant=3)
    sched.pipeline._frontier.record_outcome = AsyncMock()
    sched.pipeline._frontier.get_prefilter_context = MagicMock(return_value=MagicMock())
    sched._checkpoint = AsyncMock()

    calls = 0

    async def _analyze(_page, _goal_arg):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        sched.run_state.progress.relevant_found += 1  # worst case: every page counts

    sched.pipeline._analysis.analyzer = MagicMock(analyze=_analyze)

    def _fetched(i):
        url = URL(raw=f"https://x.test/{i}", canonical=f"https://x.test/{i}", url_key=f"k{i}", reg_domain="x.test")
        return FetchedPage(MagicMock(item_id="i", status_code=200, raw=b"x"), Page(url_key=f"k{i}", url=url))

    pages = [_fetched(i) for i in range(20)]
    sched.pipeline._page.fetch_and_extract = AsyncMock(side_effect=pages)
    await asyncio.gather(*[sched.pipeline._run_page(_item()) for _ in range(20)])

    assert sched.run_state.limits.max_relevant <= calls <= sched.run_state.limits.max_relevant + 2


@pytest.mark.parametrize(
    "published,cutoff,analyzed",
    [
        ("2026-10-01T00:00:00+00:00", "2026-10-09T00:00:00+00:00", False),
        ("2026-10-09T00:00:00+00:00", "2026-10-09T00:00:00+00:00", True),
        ("2026-10-10T00:00:00+00:00", "2026-10-09T00:00:00+00:00", True),
        (None, "2026-10-09T00:00:00+00:00", True),
        ("2026-10-01T00:00:00+00:00", None, True),
        ("2026-10-08T20:00:00-04:00", "2026-10-09T00:00:00+00:00", True),
        ("2026-10-08T00:00:00", "2026-10-09T00:00:00+00:00", False),
    ],
)
async def test_analysis_publication_cutoff(tmp_path, published, cutoff, analyzed):
    import datetime

    from crawlme.runtime import CrawlGoal
    from crawlme.scheduler.pipelines import PipelineContext
    from crawlme.scheduler.pipelines.page import PageInput

    item = _item()
    page = Page(
        url=item.url,
        url_key=item.url_key,
        plain_text="A matching offer",
        published_at=datetime.datetime.fromisoformat(published) if published else None,
    )
    worker, _, storage = _worker(tmp_path, extract=MagicMock(return_value=page))
    worker._analysis.analyze = AsyncMock(return_value=None)
    worker._discovery.discover = AsyncMock(return_value=Harvest([]))
    goal = CrawlGoal(prompt="offers", since=datetime.datetime.fromisoformat(cutoff) if cutoff else None)
    context = PipelineContext(
        task_id="test",
        events=None,
        checkpoint=AsyncMock(),
        can_run=lambda: True,
        can_dispatch=lambda: True,
        can_analyze=lambda: True,
    )
    # The candidate has no date; the real extraction path supplies it.
    await worker.run(PageInput(item, goal), context)
    assert worker._analysis.analyze.await_count == int(analyzed)
    storage.save_page.assert_called_once_with(page)
    storage.save_analysis.assert_not_called()
    worker._discovery.discover.assert_awaited_once_with(page, item.depth)
    worker._frontier.record_outcome.assert_awaited_once_with(item, "COMPLETED")
    assert worker.run_state.progress.pages_fetched == 1
    assert not worker.run_state.work
