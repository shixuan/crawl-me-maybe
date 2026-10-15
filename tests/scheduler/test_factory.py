from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.config import Settings
from crawlme.pioneer.ranker import LLMRanker
from crawlme.scheduler.factory import _build_ranker, create_scheduler
from crawlme.scheduler.pipelines.crawl import CrawlInput


def test_ranker_is_llm(tmp_path: Path):
    """One stage is left, and the builder hands it straight back."""
    llm = MagicMock(spec=LLMRanker)
    assert _build_ranker(Settings(result_dir=tmp_path), llm=llm) is llm


def test_ranker_no_creds(tmp_path: Path):
    """No LLM, no ranker.  The engine fetches in frontier order rather
    than in an order the rule stage was measured not to improve."""
    assert _build_ranker(Settings(result_dir=tmp_path)) is None


def test_sched_no_ranker(tmp_path: Path):
    """A scheduler with no credentials still builds and still crawls."""
    sched = create_scheduler(Settings(result_dir=tmp_path, llm_api_key="", llm_base_url=""))
    assert sched.pipeline._ranking.ranker is None


def test_sched_analyzer(tmp_path: Path):
    """The factory binds the supplied analyzer to the shared result sink."""
    analyzer = MagicMock()
    sched = create_scheduler(Settings(result_dir=tmp_path), analyzer=analyzer)
    assert sched.pipeline._analysis.analyzer is analyzer
    assert sched.pipeline._checkpointer.analysis is sched.pipeline._analysis


def test_scheduler_no_analyzer(tmp_path: Path):
    """analysis_enabled off: the engine runs with the subsystem absent."""
    cfg = Settings(result_dir=tmp_path, analysis_enabled=False)
    sched = create_scheduler(cfg)
    assert sched.pipeline._analysis.analyzer is None


def test_sched_run_state(tmp_path: Path):
    """The engine and tracker use the state supplied to the factory."""
    from crawlme.runtime import Limits, Progress, RunState, Stats

    state = RunState(limits=Limits(), progress=Progress(), stats=Stats())
    sched = create_scheduler(Settings(result_dir=tmp_path), run_state=state)
    assert sched.pipeline.run_state is state
    assert sched.pipeline._tracking.state is state


@pytest.mark.parametrize("injected", [True, False])
async def test_expander_created_once(tmp_path, monkeypatch, injected):
    from crawlme.pioneer import Candidate
    from crawlme.pioneer.seed_expander import SeedExpander
    from crawlme.runtime import CrawlGoal
    from crawlme.urls import URL

    expander = SeedExpander(None)
    expander.propose = AsyncMock(return_value=[])
    builder = MagicMock(return_value=expander)
    monkeypatch.setattr(SeedExpander, "from_settings", builder)
    cfg = Settings(
        _env_file=None, result_dir=tmp_path, analysis_enabled=False, expand_seeds=True, llm_api_key="", llm_base_url=""
    )
    scheduler = create_scheduler(cfg, **({"seed_expander": expander} if injected else {}))
    goal = CrawlGoal(prompt="compiler articles")
    url = URL(raw="https://example.test", canonical="https://example.test", url_key="seed")
    try:
        for _ in range(2):
            assert await scheduler.pipeline.expand_seeds(goal, [Candidate(url=url)]) == []
        assert expander.propose.await_count == 2
        if injected:
            builder.assert_not_called()
        else:
            builder.assert_called_once_with(cfg, budget=scheduler._monitor.budget)
    finally:
        await scheduler.aclose()


@pytest.mark.parametrize(("disallow", "ignore"), [(True, False), (True, True), (False, False)])
async def test_seed_verification_robots(tmp_path, monkeypatch, disallow, ignore):
    """The assembled seed verifier uses the same access policy as crawling."""
    import sqlite3

    from crawlme.digest import FetchResult, Payload
    from crawlme.pioneer.seed_expander import SeedExpander
    from crawlme.pioneer.sources import ManualSource
    from crawlme.runtime import CrawlGoal

    url = "https://example.com/source"
    calls = []

    async def fetch(item):
        address = item.url.canonical
        calls.append(address)
        raw = b"<html><body><a href='/article'>Compiler safety</a></body></html>"
        payloads = [Payload(body=b'{"items": []}')]
        if address.endswith("/robots.txt"):
            raw = b"User-agent: *\nDisallow: /\n" if disallow else b"User-agent: *\nAllow: /\n"
            payloads = []
        return FetchResult(item_id="f", url=item.url, url_key=item.url_key, status_code=200, raw=raw, payloads=payloads)

    monkeypatch.setattr(SeedExpander, "propose", AsyncMock(return_value=[(url, "source")]))
    cfg = Settings(
        _env_file=None,
        result_dir=tmp_path,
        llm_api_key="",
        llm_base_url="",
        analysis_enabled=False,
        expand_seeds=True,
        ignore_robots=ignore,
    )
    scheduler = create_scheduler(cfg, fetcher=MagicMock(fetch=fetch, aclose=AsyncMock()))
    goal = CrawlGoal(prompt="compiler safety")
    seeds = await ManualSource(["https://example.com/start"]).discover(goal)
    try:
        kept = await scheduler.pipeline.expand_seeds(goal, seeds)
        assert bool(kept) == (ignore or not disallow)
        assert (url in calls) == (ignore or not disallow)
        assert ("https://example.com/robots.txt" in calls) == (not ignore)
        if disallow and not ignore:
            assert scheduler.pipeline.run_state.rejected_seeds == [(url, "blocked by robots")]
        else:
            assert len(list(tmp_path.glob("*/raw/*/*.payload.0"))) == 1
    finally:
        await scheduler.aclose()
    with sqlite3.connect(next(tmp_path.glob("*/db/crawl.db"))) as con:
        assert con.execute("SELECT count(*) FROM pages").fetchone()[0] == 0


async def test_seed_failure(tmp_path, monkeypatch):
    import asyncio
    import json
    import sqlite3

    import httpx

    from crawlme.llm import Client
    from crawlme.pioneer.sources import ManualSource
    from crawlme.runtime import CrawlGoal, CrawlTask

    calls = []

    async def complete(self, *args, **kwargs):
        calls.append(self._stage)
        response = httpx.Response(
            402,
            json={"error": {"message": "Insufficient credits"}},
            request=httpx.Request("POST", "https://provider.test"),
        )
        raise httpx.HTTPStatusError("Payment required", request=response.request, response=response)

    monkeypatch.setattr(Client, "_complete", complete)
    cfg = Settings(
        _env_file=None,
        result_dir=tmp_path,
        llm_api_key="stub-key",
        llm_base_url="",
        llm_model="provider/model",
        analysis_enabled=False,
        expand_seeds=True,
        ignore_robots=True,
    )
    goal = CrawlGoal(prompt="compiler safety")
    task = CrawlTask(goal_id=goal.goal_id)
    fetcher = MagicMock(fetch=AsyncMock(side_effect=RuntimeError("Unexpected fetch")), aclose=AsyncMock())
    scheduler = create_scheduler(cfg, goal=goal, fetcher=fetcher)
    seeds = await ManualSource(["https://example.com/start"]).discover(goal)
    from crawlme.llm import RemoteError

    with pytest.raises(RemoteError):
        await scheduler.pipeline.expand_seeds(goal, seeds)
    await scheduler.pipeline.ingest_seeds(goal, seeds)
    await asyncio.wait_for(scheduler.run(CrawlInput(goal, task)), 2)
    assert task.state == "FAILED" and task.stopping_reason == "FATAL"
    assert calls == ["seeds"] and scheduler.pipeline.run_state.progress.pages_fetched == 0
    assert "Insufficient credits" in scheduler.pipeline.run_state.progress.fatal_error
    fetcher.fetch.assert_not_awaited()
    with sqlite3.connect(scheduler.pipeline._storage.db_path) as db:
        saved = json.loads(db.execute("SELECT checkpoint_json FROM checkpoints").fetchone()[0])
    assert not saved["state"]["progress"]["fatal_error"]
