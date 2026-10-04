from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.config import Settings
from crawlme.pioneer.ranker import LLMRanker
from crawlme.scheduler.factory import _build_ranker, create_scheduler


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
    assert sched._ranking.ranker is None


def test_sched_analyzer(tmp_path: Path):
    """A passed analyzer reaches the engine, which binds its sink."""
    analyzer = MagicMock()
    sched = create_scheduler(Settings(result_dir=tmp_path), analyzer=analyzer)
    assert sched._analysis.analyzer is analyzer
    analyzer.bind_sink.assert_called_once()


def test_scheduler_no_analyzer(tmp_path: Path):
    """analysis_enabled off: the engine runs with the subsystem absent."""
    cfg = Settings(result_dir=tmp_path, analysis_enabled=False)
    sched = create_scheduler(cfg)
    assert sched._analysis.analyzer is None


def test_sched_run_state(tmp_path: Path):
    """The engine and tracker use the state supplied to the factory."""
    from crawlme.runtime.state import Limits, Progress, RunState, Stats

    state = RunState(limits=Limits(), progress=Progress(), stats=Stats())
    sched = create_scheduler(Settings(result_dir=tmp_path), run_state=state)
    assert sched.run_state is state
    assert sched._tracking.state is state


@pytest.mark.parametrize(("disallow", "ignore"), [(True, False), (True, True), (False, False)])
async def test_seed_verification_robots(tmp_path, monkeypatch, disallow, ignore):
    """The assembled seed verifier uses the same access policy as crawling."""
    import sqlite3

    from crawlme.pioneer.seed_expander import SeedExpander
    from crawlme.pioneer.sources.manual import ManualSource
    from crawlme.schemas import CrawlGoal, FetchResult, Payload

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
        kept = await scheduler.expand_seeds(goal, seeds)
        assert bool(kept) == (ignore or not disallow)
        assert (url in calls) == (ignore or not disallow)
        assert ("https://example.com/robots.txt" in calls) == (not ignore)
        if disallow and not ignore:
            assert scheduler.run_state.rejected_seeds == [(url, "blocked by robots")]
        else:
            assert len(list(tmp_path.glob("*/raw/*/*.payload.0"))) == 1
    finally:
        await scheduler.aclose()
    with sqlite3.connect(next(tmp_path.glob("*/db/crawl.db"))) as con:
        assert con.execute("SELECT count(*) FROM pages").fetchone()[0] == 0
