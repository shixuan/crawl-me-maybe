import asyncio
from unittest.mock import AsyncMock

import pytest

from crawlme.errors import Error, ErrorHandler, ErrorPolicy, Kind, Skipped
from crawlme.llm import RemoteError


def transient():
    return Error("offline", source="fetch", code="connection_lost", kind=Kind.UNAVAILABLE)


async def test_retry_then_success():
    call = AsyncMock(side_effect=[transient(), transient(), "page"])
    result = await ErrorHandler(ErrorPolicy(retry_delay=0)).call("fetch", call, repeatable=True)
    assert result == "page" and call.await_count == 3


async def test_attempt_limit():
    call = AsyncMock(side_effect=transient())
    result = await ErrorHandler(ErrorPolicy(retry_delay=0)).call(
        "fetch",
        call,
        repeatable=True,
        optional=True,
        max_attempts=2,
    )
    assert isinstance(result, Skipped)
    assert result.attempts == call.await_count == 2


async def test_skip_keeps_cause():
    original = OSError("offline")
    error = transient()

    async def fail():
        raise error from original

    error_handler = ErrorHandler(ErrorPolicy(max_attempts=1))
    result = await error_handler.call("fetch", fail, optional=True)
    assert isinstance(result, Skipped)
    assert result.error is error and result.error.__cause__ is original
    assert await error_handler.call("next", AsyncMock(return_value="ok")) == "ok"


@pytest.mark.parametrize("kind", [Kind.RESOURCE_EXHAUSTED, Kind.TIMEOUT, Kind.UNAVAILABLE, Kind.RATE_LIMITED])
async def test_abort_closes_admission(kind):
    error = RemoteError("service failed", kind=kind)
    error_handler = ErrorHandler(ErrorPolicy(retry_delay=0))
    with pytest.raises(RemoteError) as caught:
        await error_handler.call("analysis", AsyncMock(side_effect=error), optional=True, repeatable=True)
    assert caught.value is error
    later = AsyncMock()
    with pytest.raises(RemoteError):
        await error_handler.call("ranking", later)
    later.assert_not_awaited()
    assert await ErrorHandler().call("other_run", AsyncMock(return_value="ok")) == "ok"


async def test_unknown_error_aborts():
    original = AttributeError("broken implementation")
    with pytest.raises(Error) as caught:
        await ErrorHandler().call("discovery", AsyncMock(side_effect=original), optional=True, repeatable=True)
    assert caught.value.__cause__ is original
    assert caught.value.kind == Kind.UNKNOWN


async def test_watch_cancels_work():
    error_handler = ErrorHandler()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    run = asyncio.create_task(error_handler.watch(work()))
    await entered.wait()
    failure = RuntimeError("database failed")
    error_handler.abort(failure, "persist")
    with pytest.raises(Error) as caught:
        await asyncio.wait_for(run, 1)
    assert caught.value.__cause__ is failure
    assert cancelled.is_set()


async def test_cancel_propagates():
    entered = asyncio.Event()

    async def work():
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(ErrorHandler().watch(work()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_nonrepeatable_called_once():
    call = AsyncMock(side_effect=transient())
    with pytest.raises(Error):
        await ErrorHandler().call("persist", call)
    assert call.await_count == 1
