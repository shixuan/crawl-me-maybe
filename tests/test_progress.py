"""Exercise terminal activity across logs, concurrency and cancellation."""

import asyncio
import io
import logging
from unittest.mock import Mock

import pytest

from crawlme.config import Settings
from crawlme.logging.config import setup_logging
from crawlme.logging.progress import ProgressHandler, activity


@pytest.fixture
def console(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    handler = ProgressHandler(stream)
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [handler])
    monkeypatch.setattr(root, "level", logging.INFO)
    root.manager._clear_cache()
    yield handler, stream
    handler.close()
    root.manager._clear_cache()


async def test_concurrent_cancel(console):
    handler, stream = console

    @activity("analyze")
    async def work():
        await asyncio.Event().wait()

    tasks = [asyncio.create_task(work()) for _ in range(2)]
    await asyncio.sleep(0)
    assert "analyze  2 running" in stream.getvalue()
    before = stream.getvalue()
    await asyncio.sleep(0.18)
    assert stream.getvalue() != before
    logging.getLogger().info("page complete")
    assert "page complete" in stream.getvalue()
    assert stream.getvalue().rfind("analyze") > stream.getvalue().rfind("page complete")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert not handler.active
    assert not handler.live.is_started


async def test_failure_cleans_status(console):
    handler, stream = console

    @activity("dedup")
    async def work():
        raise ValueError("invalid groups")

    with pytest.raises(ValueError):
        await work()
    assert "dedup" in stream.getvalue()
    assert not handler.active
    assert not handler.live.is_started


async def test_file_log_stays_plain(console, tmp_path):
    from crawlme.logging.config import to_file

    path = tmp_path / "run.log"
    to_file(str(path))

    @activity("fetch")
    async def work():
        logging.getLogger().info("fetched page")

    await work()
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            handler.close()
    text = path.read_text()
    assert "fetched page" in text
    assert "running" not in text
    assert "\033" not in text


async def test_close_during_activity(console):
    handler, stream = console
    token = handler.begin("fetch")
    handler.close()
    before = stream.getvalue()
    handler.end(token)
    await asyncio.sleep(0.18)
    assert stream.getvalue() == before
    assert not handler.live.is_started


async def test_log_is_one_frame(console):
    handler, stream = console
    token = handler.begin("analyze")
    stream.write = Mock(wraps=stream.write)
    logging.getLogger().info("page complete")
    stream.write.assert_called_once()
    frame = stream.write.call_args.args[0]
    assert frame.index("page complete") < frame.index("analyze")
    handler.end(token)


@pytest.mark.parametrize(
    "tty,fmt,level,term,enabled",
    [
        (True, "text", "INFO", "xterm", True),
        (False, "text", "INFO", "xterm", False),
        (True, "json", "INFO", "xterm", False),
        (True, "text", "WARNING", "xterm", False),
        (True, "text", "OFF", "xterm", False),
        (True, "text", "INFO", "dumb", False),
    ],
)
def test_console_modes(monkeypatch, tty, fmt, level, term, enabled):
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: tty)
    monkeypatch.setattr("sys.stderr", stream)
    monkeypatch.setenv("TERM", term)
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)
    setup_logging(Settings(_env_file=None, log_format=fmt, log_level=level))
    assert any(isinstance(h, ProgressHandler) for h in root.handlers) == enabled
    for handler in root.handlers:
        handler.close()


@pytest.mark.parametrize("fatal", [False, True])
async def test_provider_failure_visible(console, fatal):
    from unittest.mock import AsyncMock

    from crawlme.errors import ErrorHandler, ErrorPolicy, Kind
    from crawlme.llm import RemoteError

    handler, stream = console
    failure = RemoteError(
        "Service is too busy" if not fatal else "Account balance exhausted",
        stage="seeds",
        model="provider/model",
        status_code=503 if not fatal else 402,
        kind=Kind.UNAVAILABLE if not fatal else Kind.RESOURCE_EXHAUSTED,
    )
    call = AsyncMock(side_effect=failure)

    @activity("seed expander")
    async def work():
        return await ErrorHandler(ErrorPolicy(max_attempts=2, retry_delay=0)).call(
            "seeds",
            call,
            optional=True,
            repeatable=True,
        )

    with pytest.raises(RemoteError):
        await work()
    assert "stopping the run" in stream.getvalue()
    if fatal:
        assert call.await_count == 1
    else:
        assert "retrying in 0s" in stream.getvalue()
        assert "attempt 2" in stream.getvalue()
        assert "skipping this operation" not in stream.getvalue()
        assert call.await_count == 2
    assert failure.message in stream.getvalue()
    assert str(failure.status_code) in stream.getvalue()
    assert not handler.active and not handler.live.is_started
