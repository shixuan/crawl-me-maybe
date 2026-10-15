"""Retry, persistence and recovery at the analysis worker boundary."""

import asyncio
from unittest.mock import MagicMock

import pytest

from crawlme.analysis import AnalysisResult, Analyzer
from crawlme.errors import Error, ErrorHandler, ErrorPolicy, Kind, Skipped
from crawlme.llm import RemoteError, TokenBudgetError
from crawlme.scheduler.workers import AnalysisWorker
from tests.analysis.test_analyzer import _goal, _page, _resp, _StubClient, _valid_json

_workers = []


def _analyzer(client, *, retry_delay=0.0, max_attempts=3):
    worker = AnalysisWorker(
        Analyzer(client),
        concurrency=2,
        storage=MagicMock(),
        error_handler=ErrorHandler(ErrorPolicy(retry_delay=retry_delay, max_attempts=max_attempts)),
    )
    _workers.append(worker)
    return worker


@pytest.fixture(autouse=True)
async def close_workers():
    yield
    for worker in _workers:
        await worker.aclose()
    _workers.clear()


@pytest.mark.asyncio
async def test_parked_retry():
    client = _StubClient([RemoteError("provider down", kind=Kind.UNAVAILABLE), _resp(_valid_json())])
    analyzer = _analyzer(client)
    published: list = []
    analyzer._storage.save_analysis.side_effect = lambda row: published.append(AnalysisResult.model_validate(row))

    assert await analyzer.analyze(_page(), _goal()) is None
    for _ in range(100):
        if published:
            break
        await asyncio.sleep(0.01)

    assert len(client.calls) == 2
    assert len(published) == 1
    assert published[0].classification == "RELEVANT"


@pytest.mark.asyncio
async def test_gives_up():
    client = _StubClient(
        [
            RemoteError("down", kind=Kind.UNAVAILABLE),
            RemoteError("down", kind=Kind.UNAVAILABLE),
            RemoteError("down", kind=Kind.UNAVAILABLE),
        ]
    )
    analyzer = _analyzer(client)
    published: list = []
    analyzer._storage.save_analysis.side_effect = lambda row: published.append(AnalysisResult.model_validate(row))

    assert await analyzer.analyze(_page(), _goal()) is None
    for _ in range(100):
        if len(client.calls) >= 3:
            break
        await asyncio.sleep(0.01)
    with pytest.raises(RemoteError):
        await analyzer.drain_pending()

    assert len(client.calls) == 3
    assert published == []


@pytest.mark.asyncio
async def test_budget_no_retry():
    client = _StubClient([TokenBudgetError("budget exhausted")])
    analyzer = _analyzer(client)

    assert isinstance(await analyzer.analyze(_page(), _goal()), Skipped)
    await asyncio.sleep(0.05)

    assert len(client.calls) == 1  # no background retry behind a dead budget


@pytest.mark.asyncio
async def test_bad_json_fails():
    client = _StubClient([_resp("not json"), _resp(_valid_json())])
    analyzer = _analyzer(client)
    published: list = []
    analyzer._storage.save_analysis.side_effect = lambda row: published.append(AnalysisResult.model_validate(row))

    assert await analyzer.analyze(_page(), _goal()) is None
    for _ in range(100):
        if published:
            break
        await asyncio.sleep(0.01)

    assert published  # retry succeeded


@pytest.mark.asyncio
async def test_aclose_cancels():
    client = _StubClient([RemoteError("down", kind=Kind.UNAVAILABLE), _resp(_valid_json())])
    analyzer = _analyzer(client, retry_delay=60.0)

    assert await analyzer.analyze(_page(), _goal()) is None
    await analyzer.aclose()
    await asyncio.sleep(0.05)

    assert len(client.calls) == 1  # parked page dropped, never retried


@pytest.mark.asyncio
async def test_drain_waits():
    client = _StubClient([RemoteError("provider down", kind=Kind.UNAVAILABLE), _resp(_valid_json())])
    analyzer = _analyzer(client)
    published: list = []
    analyzer._storage.save_analysis.side_effect = lambda row: published.append(AnalysisResult.model_validate(row))

    assert await analyzer.analyze(_page(), _goal()) is None
    await analyzer.drain_pending()

    assert len(published) == 1
    assert not analyzer.pending_keys


@pytest.mark.asyncio
async def test_drain_giveup():
    client = _StubClient([RemoteError("down", kind=Kind.UNAVAILABLE), RemoteError("down", kind=Kind.UNAVAILABLE)])
    analyzer = _analyzer(client, max_attempts=2)
    published: list = []
    analyzer._storage.save_analysis.side_effect = lambda row: published.append(AnalysisResult.model_validate(row))

    assert await analyzer.analyze(_page(), _goal()) is None
    with pytest.raises(RemoteError):
        await analyzer.drain_pending()

    assert len(client.calls) == 2
    assert published == []
    assert analyzer.pending_keys == {_page().url_key}


@pytest.mark.asyncio
async def test_sink_first_try():
    client = _StubClient([_resp(_valid_json())])
    analyzer = _analyzer(client)
    published: list = []
    analyzer._storage.save_analysis.side_effect = lambda row: published.append(AnalysisResult.model_validate(row))

    result = await analyzer.analyze(_page(), _goal())
    assert result is not None
    assert published == [result]


async def test_checkpoint_retains_retry():
    entered = asyncio.Event()
    calls = 0

    class Client:
        async def chat(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RemoteError("retry", kind=Kind.UNAVAILABLE)
            if calls == 2:
                entered.set()
                await asyncio.Event().wait()
            return _resp(_valid_json())

    analyzer = _analyzer(Client())
    results = []
    analyzer._storage.save_analysis.side_effect = lambda row: results.append(AnalysisResult.model_validate(row))
    try:
        await analyzer.analyze(_page(), _goal())
        await asyncio.wait_for(entered.wait(), 2)
        saved = analyzer.dump()
        await analyzer.aclose()
        assert saved["entries"][0]["attempts"] == 1
        await asyncio.sleep(0)
        assert calls == 2 and not results
        analyzer = _analyzer(Client())
        analyzer._storage.save_analysis.side_effect = lambda row: results.append(AnalysisResult.model_validate(row))
        analyzer.prepare(_goal(), "task")
        analyzer.restore(saved)
        analyzer.start_retries()
        await asyncio.wait_for(analyzer.drain_pending(), 2)
        assert len(results) == 1 and calls == 3
    finally:
        await analyzer.aclose()
    assert analyzer._pending.empty()


async def test_retry_failure_surfaces():
    analyzer = _analyzer(_StubClient([RemoteError("retry", kind=Kind.UNAVAILABLE), RuntimeError("broken retry")]))
    try:
        await analyzer.analyze(_page(), _goal())
        with pytest.raises(Error, match="broken retry"):
            await asyncio.wait_for(analyzer.drain_pending(), 2)
    finally:
        await analyzer.aclose()
