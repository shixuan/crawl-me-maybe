from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.config import Settings
from crawlme.pioneer.ranker import LLMRanker
from crawlme.scheduler.pipelines.crawl import CrawlInput
from tests.scheduler.helpers import create_crawl


def test_sched_analyzer(tmp_path: Path):
    """The factory binds the supplied analyzer to the shared result sink."""
    analyzer = MagicMock()
    sched = create_crawl(Settings(result_dir=tmp_path), analyzer=analyzer)
    assert sched.pipeline._page._analysis.analyzer is analyzer
    assert sched.pipeline._checkpointer.analysis is sched.pipeline._page._analysis


def test_sched_run_state(tmp_path: Path):
    """The engine and tracker use the state supplied to the factory."""
    from crawlme.runtime import Limits, Progress, RunState, Stats

    state = RunState(limits=Limits(), progress=Progress(), stats=Stats())
    sched = create_crawl(Settings(result_dir=tmp_path), run_state=state)
    assert sched.pipeline.run_state is state
    assert sched.pipeline._tracking.state is state
    assert sched.engine._monitor.runtime is state
    assert sched.engine._monitor.frontier is sched.pipeline._frontier
    assert sched.engine._monitor.budget is sched.pipeline._budget
    assert sched.pipeline._checkpointer.runtime is state
    assert sched.pipeline._checkpointer.frontier is sched.engine._monitor.frontier
    assert sched.pipeline._checkpointer.budget is sched.engine._monitor.budget
    assert sched.engine._save == sched.pipeline.save_checkpoint


@pytest.mark.parametrize("injected", [True, False])
async def test_expander_created_once(tmp_path, monkeypatch, injected):
    from crawlme.pioneer import Candidate
    from crawlme.pioneer.seed_expander import SeedExpander
    from crawlme.runtime import Goal
    from crawlme.urls import URL

    expander = SeedExpander(None)
    expander.propose = AsyncMock(return_value=[])
    builder = MagicMock(return_value=expander)
    monkeypatch.setattr(SeedExpander, "from_settings", builder)
    cfg = Settings(
        _env_file=None, result_dir=tmp_path, analysis_enabled=False, expand_seeds=True, llm_api_key="", llm_base_url=""
    )
    scheduler = create_crawl(cfg, **({"seed_expander": expander} if injected else {}))
    goal = Goal(prompt="compiler articles")
    url = URL(raw="https://example.test", canonical="https://example.test", url_key="seed")
    try:
        for _ in range(2):
            assert await scheduler.pipeline.expand_seeds(goal, [Candidate(url=url)]) == []
        assert expander.propose.await_count == 2
        if injected:
            builder.assert_not_called()
        else:
            builder.assert_called_once_with(cfg, budget=scheduler.engine._monitor.budget)
    finally:
        await scheduler.aclose()


@pytest.mark.parametrize(("disallow", "ignore"), [(True, False), (True, True), (False, False)])
async def test_seed_verification_robots(tmp_path, monkeypatch, disallow, ignore):
    """The assembled seed verifier uses the same access policy as crawling."""
    import sqlite3

    from crawlme.digest import FetchResult, Payload
    from crawlme.pioneer.seed_expander import SeedExpander
    from crawlme.pioneer.sources import ManualSource
    from crawlme.runtime import Goal

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
    scheduler = create_crawl(cfg, fetcher=MagicMock(fetch=fetch, aclose=AsyncMock()))
    goal = Goal(prompt="compiler safety")
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
    from crawlme.runtime import Goal, Task

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
    goal = Goal(prompt="compiler safety")
    task = Task(goal_id=goal.goal_id)
    fetcher = MagicMock(fetch=AsyncMock(side_effect=RuntimeError("Unexpected fetch")), aclose=AsyncMock())
    scheduler = create_crawl(cfg, goal=goal, fetcher=fetcher)
    seeds = await ManualSource(["https://example.com/start"]).discover(goal)
    from crawlme.llm import RemoteError

    with pytest.raises(RemoteError):
        await scheduler.pipeline.expand_seeds(goal, seeds)
    await scheduler.pipeline.ingest_seeds(goal, seeds)
    await asyncio.wait_for(scheduler.engine.run(CrawlInput(goal, task)), 2)
    assert task.state == "FAILED" and task.stopping_reason == "FATAL"
    assert calls == ["seeds"] and scheduler.pipeline.run_state.progress.pages_fetched == 0
    assert "Insufficient credits" in scheduler.pipeline.run_state.progress.fatal_error
    fetcher.fetch.assert_not_awaited()
    with sqlite3.connect(scheduler.pipeline._storage.db_path) as db:
        saved = json.loads(db.execute("SELECT checkpoint_json FROM checkpoints").fetchone()[0])
    assert not saved["state"]["progress"]["fatal_error"]


@pytest.mark.parametrize("enabled", [True, False])
def test_ranking_budget(tmp_path, monkeypatch, enabled):
    from crawlme.llm import TokenBudget

    budget = TokenBudget(100)
    ranker = MagicMock(spec=LLMRanker)
    build = MagicMock(return_value=ranker)
    monkeypatch.setattr(LLMRanker, "from_settings", build)
    scheduler = create_crawl(
        Settings(result_dir=tmp_path, llm_api_key="", llm_base_url=""),
        budget=budget,
        ranking_enabled=enabled,
    )
    assert scheduler.pipeline._ranking.ranker is (ranker if enabled else None)
    if enabled:
        assert build.call_args.kwargs["budget"] is budget
    else:
        build.assert_not_called()


def test_replay_shared_dependencies(tmp_path, monkeypatch):
    from crawlme.llm import TokenBudget
    from crawlme.persist import Storage
    from crawlme.scheduler.pipelines import factory

    budget = TokenBudget(100)
    storage = Storage(str(tmp_path / "crawl.db"), str(tmp_path / "raw"))
    builders = []
    for component in (factory.Analyzer, factory.Grouper, factory.GoalEnhancer):
        build = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(component, "from_settings", build)
        builders.append(build)
    pipeline = factory.create_replay(Settings(result_dir=tmp_path), storage=storage, budget=budget)
    for build in builders:
        assert build.call_args.kwargs["budget"] is budget
    assert pipeline._analysis.error_handler is pipeline._dedup.error_handler
    assert pipeline._analysis._storage is storage
    assert pipeline._dedup._storage is storage


@pytest.mark.parametrize("name", ["create_crawl", "create_dedup"])
def test_unknown_dependency(tmp_path, monkeypatch, name):
    from crawlme.llm import TokenBudget
    from crawlme.scheduler.pipelines import factory

    storage_builder = MagicMock()
    grouper_builder = MagicMock()
    monkeypatch.setattr(factory.Storage, "create", storage_builder)
    monkeypatch.setattr(factory.Grouper, "from_settings", grouper_builder)
    kwargs = {"storage": MagicMock(), "budget": TokenBudget(100)} if name == "create_dedup" else {}
    with pytest.raises(TypeError, match="groupr"):
        getattr(factory, name)(Settings(result_dir=tmp_path), groupr=MagicMock(), **kwargs)
    storage_builder.assert_not_called()
    grouper_builder.assert_not_called()


@pytest.mark.parametrize("component", ["ranker", "seed_expander", "grouper"])
@pytest.mark.parametrize("selection", ["default", "disabled", "injected"])
def test_optional_dependency(tmp_path, monkeypatch, component, selection):
    from crawlme.llm import TokenBudget
    from crawlme.scheduler.pipelines import factory

    cfg = Settings(_env_file=None, result_dir=tmp_path, expand_seeds=True, llm_api_key="", llm_base_url="")
    budget = TokenBudget(100)
    components = {"ranker": factory.LLMRanker, "seed_expander": factory.SeedExpander, "grouper": factory.Grouper}
    builders = {}
    for name, cls in components.items():
        builder = MagicMock(return_value=MagicMock(spec=cls))
        monkeypatch.setattr(cls, "from_settings", builder)
        builders[name] = builder
    injected = MagicMock(spec=components[component])
    kwargs = {} if selection == "default" else {component: None if selection == "disabled" else injected}
    pipeline = factory.create_crawl(cfg, budget=budget, storage=MagicMock(), ranking_enabled=True, **kwargs)
    actual = {
        "ranker": pipeline._ranking.ranker,
        "seed_expander": pipeline._seed_expander,
        "grouper": pipeline._dedup.grouper,
    }[component]
    if selection == "default":
        assert actual is builders[component].return_value
        builders[component].assert_called_once_with(cfg, budget=budget)
    else:
        assert actual is (None if selection == "disabled" else injected)
        builders[component].assert_not_called()


def test_injected_workers(tmp_path, monkeypatch):
    from crawlme.scheduler.pipelines import factory

    builders = []
    for owner, name in (
        (factory, "_build_fetcher"),
        (factory, "_build_harvester"),
        (factory, "create_dedup"),
        (factory.Analyzer, "from_settings"),
        (factory.GoalEnhancer, "from_settings"),
    ):
        builder = MagicMock()
        monkeypatch.setattr(owner, name, builder)
        builders.append(builder)
    fetch = MagicMock(spec=factory.FetchWorker)
    discovery = MagicMock(spec=factory.DiscoveryWorker)
    analysis = MagicMock(spec=factory.AnalysisWorker, analyzer=None)
    enhancer = MagicMock(spec=factory.GoalEnhancer)
    dedup = MagicMock(spec=factory.DedupPipeline, grouper=None)
    pipeline = factory.create_crawl(
        Settings(result_dir=tmp_path),
        storage=MagicMock(),
        fetch=fetch,
        discovery=discovery,
        analysis=analysis,
        goal_enhancer=enhancer,
        dedup=dedup,
        ranker=None,
        seed_expander=None,
    )
    assert pipeline._page._fetch is fetch
    assert pipeline._page._discovery is discovery
    assert pipeline._page._analysis is pipeline._checkpointer.analysis is analysis
    assert pipeline._goal_enhancer is enhancer
    assert pipeline._dedup is dedup
    for builder in builders:
        builder.assert_not_called()


@pytest.mark.parametrize("matching", [True, False])
def test_tracker_state_identity(tmp_path, monkeypatch, matching):
    from crawlme.runtime import Limits, Progress, RunState, Stats, Tracker
    from crawlme.scheduler.pipelines import factory

    state = RunState(limits=Limits(), progress=Progress(), stats=Stats())
    other = state if matching else RunState(limits=Limits(), progress=Progress(), stats=Stats())
    tracker = Tracker(other)
    storage_builder = MagicMock()
    monkeypatch.setattr(factory.Storage, "create", storage_builder)
    if matching:
        pipeline = factory.create_crawl(Settings(result_dir=tmp_path), tracking=tracker, run_state=state)
        assert pipeline.run_state is state
        assert pipeline._page._tracking is tracker
        assert pipeline._checkpointer.runtime is state
        storage_builder.assert_called_once()
    else:
        with pytest.raises(ValueError, match=r"run_state must be tracking\.state"):
            factory.create_crawl(Settings(result_dir=tmp_path), tracking=tracker, run_state=state)
        storage_builder.assert_not_called()
