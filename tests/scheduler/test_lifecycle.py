from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.analysis import PageAnalyzer
from crawlme.config import Settings
from crawlme.llm import LLMError, LLMResponse
from crawlme.pioneer.sources.manual import ManualSource
from crawlme.scheduler.factory import create_scheduler
from crawlme.schemas import CrawlGoal, CrawlTask, FetchResult


@pytest.mark.parametrize("dedup", [False, True])
@pytest.mark.parametrize("stop", [None, "tokens", "time", "user", "deadline"])
async def test_retries_ignore_dedup(tmp_path, monkeypatch, dedup, stop):
    """Grouping cannot affect retry completion or bounded shutdown."""
    settling = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def chat(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LLMError("temporary failure")
        await release.wait()
        return LLMResponse('{"classification":"RELEVANT","relevance_score":0.9}', 1, 1, "stub")

    class Analyzer(PageAnalyzer):
        async def drain_pending(self):
            settling.set()
            if stop is None:
                release.set()
            await super().drain_pending()

    analyzer = Analyzer(MagicMock(chat=chat), retry_delay=0)
    cfg = Settings(_env_file=None, result_dir=tmp_path, llm_api_key="", llm_base_url="", ignore_robots=True)
    goal = CrawlGoal(prompt="compiler safety", max_pages=1)
    if stop == "time":
        goal.max_duration_sec = 60
    if stop == "deadline":
        monkeypatch.setattr("crawlme.scheduler.engine._SETTLE_TIMEOUT", 0.05)
    task = CrawlTask(goal_id=goal.goal_id)

    async def fetch(item):
        return FetchResult(
            item_id="f",
            url_key=item.url_key,
            url=item.url,
            status_code=200,
            raw=b"<html><body><p>Compiler safety and memory ownership.</p></body></html>",
        )

    grouped = []

    async def group(goal, rows):
        assert analyzer._drain_task is None
        grouped.extend(rows)
        return []

    scheduler = create_scheduler(
        cfg,
        goal=goal,
        analyzer=analyzer,
        fetcher=MagicMock(fetch=fetch, aclose=AsyncMock()),
        grouper=MagicMock(group=group) if dedup else None,
    )
    seeds = await ManualSource(["https://example.com/article"]).discover(goal)
    await scheduler.ingest_seeds(goal, seeds)
    run = asyncio.create_task(scheduler.run(goal, task))
    try:
        if stop is not None:
            await asyncio.wait_for(settling.wait(), timeout=3)
            if stop == "tokens":
                scheduler.note_tokens_used(goal.max_tokens)
            elif stop == "time":
                scheduler.run_state.progress.started_at -= 61
            elif stop == "user":
                await scheduler.stop()
        await asyncio.wait_for(run, timeout=10 if stop is None else 3)
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await scheduler.aclose()
    with sqlite3.connect(next(tmp_path.glob("*/db/crawl.db"))) as con:
        assert con.execute("SELECT count(*) FROM analyses").fetchone()[0] == int(stop is None)
    assert calls == 2
    assert len(grouped) == int(dedup and stop is None)
    assert analyzer._drain_task is None
