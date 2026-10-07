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


async def test_elapsed_and_stages(console, monkeypatch):
    handler, stream = console
    monkeypatch.setattr("crawlme.logging.progress.time.monotonic", lambda: 10)
    first = handler.begin("fetch")
    monkeypatch.setattr("crawlme.logging.progress.time.monotonic", lambda: 76)
    second = handler.begin("analyze")
    assert "fetch  1 running · oldest 1m 06s" in stream.getvalue()
    handler.end(first)
    assert len(handler.active) == 1
    handler.end(second)


async def test_refresh_uses_rich(console):
    handler, stream = console
    token = handler.begin("analyze")
    stream.write = Mock(wraps=stream.write)
    handler.live.refresh()
    stream.write.assert_called_once()
    frame = stream.write.call_args.args[0]
    assert "analyze" in frame
    handler.end(token)


async def test_log_is_one_frame(console):
    handler, stream = console
    token = handler.begin("analyze")
    stream.write = Mock(wraps=stream.write)
    logging.getLogger().info("page complete")
    stream.write.assert_called_once()
    frame = stream.write.call_args.args[0]
    assert frame.index("page complete") < frame.index("analyze")
    handler.end(token)


async def test_progress_color(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.delenv("NO_COLOR", raising=False)
    handler = ProgressHandler(stream, color=True)
    try:
        token = handler.begin("analyze")
        assert "\033[38;2;224;126;164m" in stream.getvalue()
        assert "analyze" in stream.getvalue()
        stream.seek(0)
        stream.truncate()
        handler.live.refresh()
        assert "\033[38;2;224;126;164m" in stream.getvalue()
        handler.end(token)
    finally:
        handler.close()


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
