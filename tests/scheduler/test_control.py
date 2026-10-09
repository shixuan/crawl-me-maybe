"""Exercise lifecycle boundaries through both pumps and real run storage."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.analysis import PageAnalyzer
from crawlme.config import Settings
from crawlme.discovery.harvester import Harvest
from crawlme.llm import LLMError, LLMResponse
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.scheduler.factory import create_scheduler
from crawlme.schemas import Candidate, CrawlGoal, CrawlTask, FetchResult, Page, RankDecision


def candidate(path="child", **kwargs):
    url = Canonicalizer().canonicalize(f"https://example.com/{path}", "https://example.com/")
    return Candidate(url=url, seed_url_key="seed", **kwargs)


def build(tmp_path, **overrides):
    fetched = []

    async def fetch(item):
        fetched.append(item.url.canonical)
        return FetchResult(item_id=item.item_id, url_key=item.url_key, url=item.url, status_code=200, raw=b"article")

    def extract(result, raw_path):
        return Page(url_key=result.url_key, url=result.url, plain_text="An article about compiler correctness.")

    cfg = Settings(
        _env_file=None, result_dir=tmp_path, llm_api_key="", llm_base_url="", llm_model="", ignore_robots=True
    )
    goal = CrawlGoal(prompt="compiler correctness", max_pages=0, max_duration_sec=0, domain_budget=0)
    kwargs = dict(
        fetcher=MagicMock(fetch=fetch, aclose=AsyncMock()),
        extractor=MagicMock(extract=extract),
        harvester=MagicMock(harvest=lambda page, depth: Harvest([])),
    )
    kwargs.update(overrides)
    scheduler = create_scheduler(cfg, goal=goal, **kwargs)
    return scheduler, goal, CrawlTask(goal_id=goal.goal_id), fetched


async def wait_until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), 2)


@pytest.fixture(autouse=True)
def quick_poll(monkeypatch):
    monkeypatch.setattr("crawlme.scheduler.engine._POP_SLEEP", 0.005)


@pytest.mark.parametrize("stage", ["ranking", "dispatch", "page"])
async def test_failure_stops_both(tmp_path, stage):
    rank_entered = asyncio.Event()

    async def rank(goal, batch, history, **kwargs):
        rank_entered.set()
        if stage == "ranking":
            raise RuntimeError("ranking failed")
        await asyncio.Event().wait()

    ranker = MagicMock(rank_batch=rank, aclose=AsyncMock())
    scheduler, goal, task, _ = build(tmp_path, ranker=ranker)
    await scheduler._frontier.push_candidates([candidate()])
    if stage == "dispatch":

        async def broken_pop(**kwargs):
            await rank_entered.wait()
            raise RuntimeError("dispatch failed")

        scheduler._frontier.pop_next = broken_pop
    elif stage == "page":
        scheduler._extractor.extract = MagicMock(side_effect=RuntimeError("page failed"))
        await scheduler.ingest_seeds(goal, [candidate("seed")])
    await asyncio.wait_for(scheduler.run(goal, task), 2)
    assert task.state == "FAILED"
    assert task.stopping_reason == "FATAL"
    assert f"{stage} failed" in scheduler.run_state.progress.fatal_error
    assert not scheduler._inflight and not scheduler._pump_tasks
    assert scheduler._frontier.scoring == scheduler.run_state.progress.in_flight == 0
    ranker.aclose.assert_awaited_once()
    scheduler._fetch.fetcher.aclose.assert_awaited_once()
    with closing(sqlite3.connect(scheduler._storage.db_path)) as db:
        assert db.execute("SELECT state FROM crawl_tasks").fetchone()[0] == "FAILED"


@pytest.mark.parametrize("timeout", [False, True])
async def test_pause_ranking_and_resume(tmp_path, monkeypatch, timeout):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def rank(goal, batch, history, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        if calls == 1:
            await release.wait()
        return [RankDecision(candidate_id=c.candidate_id, url_key=c.url.url_key, priority=0.9) for c in batch]

    scheduler, goal, task, fetched = build(tmp_path, ranker=MagicMock(rank_batch=rank, aclose=AsyncMock()))
    await scheduler._frontier.push_candidates([candidate()])
    if timeout:
        monkeypatch.setattr("crawlme.scheduler.engine._SETTLE_TIMEOUT", 0.04)
    run = asyncio.create_task(scheduler.run(goal, task))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        started = scheduler.run_state.progress.started_at
        pause = asyncio.create_task(scheduler.pause())
        await wait_until(lambda: scheduler._state == "PAUSING")
        assert not pause.done()
        if not timeout:
            release.set()
        await asyncio.wait_for(pause, 2)
        assert task.state == "PAUSED" and not run.done()
        assert not scheduler._pump_tasks
        assert scheduler._frontier.scoring == 0
        assert fetched == []
        with closing(sqlite3.connect(scheduler._storage.db_path)) as db:
            assert db.execute("SELECT state FROM crawl_tasks").fetchone()[0] == "PAUSED"
            snapshot = json.loads(db.execute("SELECT snapshot_json FROM frontier_snapshots").fetchone()[0])
            assert len(snapshot["waiting"]["candidates"]) + len(snapshot["ordering"]["heap"]) == 1
        await asyncio.gather(scheduler.resume(), scheduler.resume())
        await asyncio.wait_for(run, 2)
        assert fetched == ["https://example.com/child"]
        assert calls == 1 + int(timeout)
        assert scheduler.run_state.stats.candidates_ranked == 1
        assert scheduler.run_state.progress.started_at == started
    finally:
        release.set()
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await scheduler.aclose()


async def test_stop_while_paused(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def rank(*args, **kwargs):
        entered.set()
        await release.wait()
        return []

    scheduler, goal, task, _ = build(tmp_path, ranker=MagicMock(rank_batch=rank, aclose=AsyncMock()))
    await scheduler._frontier.push_candidates([candidate()])
    run = asyncio.create_task(scheduler.run(goal, task))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        pause = asyncio.create_task(scheduler.pause())
        await wait_until(lambda: scheduler._state == "PAUSING")
        release.set()
        await asyncio.wait_for(pause, 2)
        await scheduler.stop()
        await asyncio.wait_for(run, 2)
        assert task.stopping_reason == "USER_REQUESTED"
        assert task.state == "COMPLETED"
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await scheduler.aclose()


async def test_cancel_releases_workers(tmp_path, monkeypatch):
    entered = asyncio.Event()

    async def fetch(item):
        entered.set()
        await asyncio.Event().wait()

    fetcher = MagicMock(fetch=fetch, aclose=AsyncMock())
    scheduler, goal, task, _ = build(tmp_path, fetcher=fetcher)
    await scheduler.ingest_seeds(goal, [candidate("seed")])
    monkeypatch.setattr("crawlme.scheduler.engine._SETTLE_TIMEOUT", 0.03)
    run = asyncio.create_task(scheduler.run(goal, task))
    await asyncio.wait_for(entered.wait(), 2)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 2)
    assert not scheduler._inflight and not scheduler._pump_tasks
    assert scheduler.run_state.progress.in_flight == 0
    assert task.stopping_reason == "USER_REQUESTED"
    fetcher.aclose.assert_awaited_once()


async def test_cleanup_continues_on_error(tmp_path):
    ranker = MagicMock(aclose=AsyncMock(side_effect=RuntimeError("close failed")))
    scheduler, goal, task, _ = build(tmp_path, ranker=ranker)
    await asyncio.wait_for(scheduler.run(goal, task), 2)
    assert task.state == scheduler._state == "FAILED"
    assert task.stopping_reason == "FATAL"
    scheduler._fetch.fetcher.aclose.assert_awaited_once()
    with closing(sqlite3.connect(scheduler._storage.db_path)) as db:
        assert db.execute("SELECT state FROM crawl_tasks").fetchone()[0] == "FAILED"


async def test_repeated_pause_resume(tmp_path, monkeypatch):
    entered = asyncio.Event()

    async def rank(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    scheduler, goal, task, _ = build(tmp_path, ranker=MagicMock(rank_batch=rank, aclose=AsyncMock()))
    await scheduler._frontier.push_candidates([candidate()])
    monkeypatch.setattr("crawlme.scheduler.engine._SETTLE_TIMEOUT", 0.03)
    run = asyncio.create_task(scheduler.run(goal, task))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(3):
            await asyncio.wait_for(scheduler.pause(), 2)
            assert task.state == "PAUSED" and not run.done()
            assert scheduler._frontier.waiting_size == 1
            assert not scheduler._pump_tasks
            await asyncio.wait_for(scheduler.resume(), 2)
        await scheduler.stop()
        await asyncio.wait_for(run, 2)
        assert task.state == "COMPLETED"
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)


async def test_page_retention_tracks_work(tmp_path):
    peaks = []
    limit = 80

    def harvest(page, depth):
        number = int(page.url.canonical.rsplit("/", 1)[1])
        peaks.append((len(scheduler.run_state.pages), len(scheduler.run_state.page_contexts)))
        return Harvest([candidate(str(number + 1), source_url_key=page.url_key)] if number < limit else [])

    scheduler, goal, task, fetched = build(tmp_path, harvester=MagicMock(harvest=harvest))
    await scheduler.ingest_seeds(goal, [candidate("1")])
    await asyncio.wait_for(scheduler.run(goal, task), 5)
    assert len(fetched) == limit
    assert max(p for p, _ in peaks) <= 2
    assert max(c for _, c in peaks) <= 2
    assert len(scheduler.run_state.pages) == len(scheduler.run_state.page_contexts) == 0


@pytest.mark.parametrize("rank_child", [False, True])
async def test_retry_retains_source(tmp_path, rank_child):
    retry_entered, retry_release = asyncio.Event(), asyncio.Event()
    rank_entered, rank_release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def chat(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LLMError("retry")
        retry_entered.set()
        await retry_release.wait()
        return LLMResponse(
            '{"classification":"RELEVANT","relevance_score":0.9,"summary":"Compiler safety"}', 1, 1, "stub"
        )

    async def rank(*args, **kwargs):
        rank_entered.set()
        await rank_release.wait()
        return []

    def harvest(page, depth):
        return Harvest([candidate("child", source_url_key=page.url_key)] if rank_child else [])

    analyzer = PageAnalyzer(MagicMock(chat=chat), retry_delay=0)
    scheduler, goal, task, _ = build(
        tmp_path,
        analyzer=analyzer,
        harvester=MagicMock(harvest=harvest),
        ranker=MagicMock(rank_batch=rank, aclose=AsyncMock()),
    )
    seed = candidate("seed")
    key = seed.url.url_key
    await scheduler.ingest_seeds(goal, [seed])
    run = asyncio.create_task(scheduler.run(goal, task))
    try:
        await asyncio.wait_for(retry_entered.wait(), 2)
        await wait_until(lambda: not scheduler._inflight)
        assert key in analyzer.pending_keys
        assert scheduler.run_state.pages.by_url(seed.url.canonical).seed == key
        if rank_child:
            await asyncio.wait_for(rank_entered.wait(), 2)
        retry_release.set()
        await wait_until(lambda: not analyzer.pending_keys)
        if rank_child:
            assert scheduler.run_state.page_contexts[key]["summary"] == "Compiler safety"
            assert scheduler.run_state.pages.by_url(seed.url.canonical).counted
        rank_release.set()
        await asyncio.wait_for(run, 2)
        source = scheduler.run_state.seeds[key]
        assert source.funnel.relevant == source.funnel.judged == 1
        assert list(source.window) == [True]
        assert len(scheduler.run_state.pages) == len(scheduler.run_state.page_contexts) == 0
    finally:
        retry_release.set()
        rank_release.set()
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
