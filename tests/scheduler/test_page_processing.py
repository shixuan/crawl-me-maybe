from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.config import Settings
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.scheduler.engine import FetchedPage
from crawlme.scheduler.factory import create_scheduler
from crawlme.schemas import URL, FetchResult, FrontierItem, Page, Payload


def _worker(tmp_path, *, extract=None, timeout=1):
    url = URL(raw="https://example.com/p", canonical="https://example.com/p", url_key="p", reg_domain="example.com")
    item = FrontierItem(url=url, url_key="p", reg_domain="example.com")
    result = FetchResult(
        item_id=item.item_id,
        url=url,
        url_key="p",
        status_code=200,
        raw=b"<p>body</p>",
        payloads=[Payload(url="https://example.com/api", body=b"{}")],
    )
    fetcher = MagicMock(fetch=AsyncMock(return_value=result), aclose=AsyncMock())
    storage = MagicMock()
    storage.raw_html_path.return_value = str(tmp_path / "page.html")
    storage.save_payload.return_value = str(tmp_path / "payload.json")
    page = Page(url=url, url_key="p")
    extractor = MagicMock(extract=extract or MagicMock(return_value=page))
    frontier = MagicMock(record_outcome=AsyncMock())
    worker = create_scheduler(
        Settings(result_dir=tmp_path, fetch_concurrency=1, extract_timeout=timeout, analysis_enabled=False),
        fetcher=fetcher,
        extractor=extractor,
        robots=RobotsPolicy(ignore=True),
        storage=storage,
        frontier=frontier,
    )
    return worker, item, storage


@pytest.mark.asyncio
async def test_persist_before_return(tmp_path):
    worker, item, storage = _worker(tmp_path)

    def extract(result, path):
        storage.save_raw_html.assert_called_once_with("p", item.item_id, result.raw)
        storage.save_payload.assert_not_called()
        storage.save_page.assert_not_called()
        assert path == str(tmp_path / "page.html")
        return Page(url=result.url, url_key=result.url_key, raw_html_path=path)

    worker._extractor.extract.side_effect = extract
    outcome = await worker._fetch_and_extract(item)
    assert isinstance(outcome, FetchedPage)
    assert outcome.page.payload_paths == [str(tmp_path / "payload.json")]
    storage.save_raw_html.assert_called_once_with("p", item.item_id, b"<p>body</p>")
    storage.save_page.assert_called_once_with(outcome.page)
    assert not worker._page_slots.locked()


@pytest.mark.asyncio
async def test_raw_write_failure_stops_extraction(tmp_path):
    worker, item, storage = _worker(tmp_path)
    storage.save_raw_html.side_effect = OSError("disk full")
    with pytest.raises(OSError, match="disk full"):
        await worker._fetch_and_extract(item)
    worker._extractor.extract.assert_not_called()
    storage.save_payload.assert_not_called()
    storage.save_page.assert_not_called()
    assert not worker._page_slots.locked()


@pytest.mark.asyncio
async def test_page_slot_covers_persistence(tmp_path):
    scheduler, item, _ = _worker(tmp_path)
    saving = asyncio.Event()
    release = asyncio.Event()
    second_started = asyncio.Event()

    async def save_raw(*_):
        saving.set()
        await release.wait()
        return str(tmp_path / "page.html")

    scheduler._persist.save_raw = AsyncMock(side_effect=save_raw)

    async def second_page():
        second_started.set()
        return await scheduler._fetch_and_extract(item)

    first = asyncio.create_task(scheduler._fetch_and_extract(item))
    second = None
    try:
        await asyncio.wait_for(saving.wait(), 1)
        second = asyncio.create_task(second_page())
        await asyncio.wait_for(second_started.wait(), 1)
        scheduler._fetch.fetcher.fetch.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []))
    assert scheduler._fetch.fetcher.fetch.await_count == 2


@pytest.mark.asyncio
async def test_fetch_failure_result(tmp_path):
    worker, item, storage = _worker(tmp_path)
    worker._fetch.fetcher.fetch.side_effect = OSError("offline")
    assert await worker._fetch_and_extract(item) is None
    assert worker.run_state.stats.fetch_errors == 1
    storage.save_page.assert_not_called()
    assert not worker._page_slots.locked()


@pytest.mark.asyncio
async def test_extract_timeout_result(tmp_path):
    release = threading.Event()
    finished = threading.Event()

    def extract(result, path):
        try:
            release.wait(timeout=2)
            return Page(url=result.url, url_key=result.url_key)
        finally:
            finished.set()

    worker, item, storage = _worker(tmp_path, extract=extract, timeout=0.02)
    try:
        assert await worker._fetch_and_extract(item) is None
        assert worker.run_state.progress.pages_fetched == 1
        worker._frontier.record_outcome.assert_awaited_once_with(item, "SKIPPED")
        storage.save_raw_html.assert_called_once()
        storage.save_payload.assert_not_called()
        storage.save_page.assert_not_called()
        assert not worker._page_slots.locked()
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 2)
