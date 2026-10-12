"""Exercise lifecycle boundaries through both pumps and real run storage."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.analysis import Analyzer
from crawlme.config import Settings
from crawlme.discovery import Harvest
from crawlme.llm import Error, LLMResponse
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.scheduler.factory import create_scheduler
from crawlme.schemas import Candidate, CrawlGoal, CrawlTask, FetchResult, Page


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


async def test_page_retention_tracks_work(tmp_path):
    peaks = []
    limit = 80

    def harvest(page, depth):
        number = int(page.url.canonical.rsplit("/", 1)[1])
        peaks.append((len(scheduler.run_state.pages), len(scheduler.run_state.page_contexts)))
        return Harvest([candidate(str(number + 1), source_url_key=page.url_key)] if number < limit else [])

    scheduler, goal, task, fetched = build(tmp_path, harvester=MagicMock(harvest=harvest))
    await scheduler.ingest_seeds(goal, [candidate("1")])
    await asyncio.wait_for(scheduler.run(goal, task), 15)
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
            raise Error("retry")
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

    analyzer = Analyzer(MagicMock(chat=chat), retry_delay=0)
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
