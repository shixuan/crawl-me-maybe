from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.pioneer.robots import RobotsPolicy
from crawlme.scheduler.workers import FetchedPage, FetchFailure, FetchWorker
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
    worker = FetchWorker(fetcher, extractor, RobotsPolicy(ignore=True), storage, concurrency=1, extract_timeout=timeout)
    return worker, item, storage


@pytest.mark.asyncio
async def test_persist_before_return(tmp_path):
    worker, item, storage = _worker(tmp_path)
    outcome = await worker.fetch(item)
    assert isinstance(outcome, FetchedPage)
    assert outcome.page.payload_paths == [str(tmp_path / "payload.json")]
    storage.save_raw_html.assert_called_once_with("p", item.item_id, b"<p>body</p>")
    storage.save_page.assert_called_once_with(outcome.page)
    assert not worker._slots.locked()


@pytest.mark.asyncio
async def test_fetch_failure_result(tmp_path):
    worker, item, storage = _worker(tmp_path)
    worker.fetcher.fetch.side_effect = OSError("offline")
    assert await worker.fetch(item) == FetchFailure("fetch", "OSError")
    storage.save_page.assert_not_called()
    assert not worker._slots.locked()


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
        assert await worker.fetch(item) == FetchFailure("extract_timeout")
        storage.save_page.assert_not_called()
        assert not worker._slots.locked()
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 2)
