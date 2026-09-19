from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.pioneer.robots import RobotsPolicy
from crawlme.scheduler.workers import FetchFailure, FetchWorker
from crawlme.schemas import URL, FetchResult, FrontierItem


@pytest.mark.asyncio
async def test_fetch_only_returns_response():
    url = URL(raw="https://example.com/p", canonical="https://example.com/p", url_key="p")
    item = FrontierItem(url=url, url_key="p")
    result = FetchResult(item_id="f", url=url, url_key="p", raw=b"body", status_code=200)
    fetcher = MagicMock(fetch=AsyncMock(return_value=result))
    storage = MagicMock()
    worker = FetchWorker(fetcher, RobotsPolicy(ignore=True), storage, concurrency=1)
    assert await worker.fetch(item) is result
    assert not storage.mock_calls
    fetcher.fetch.side_effect = OSError("offline")
    assert await worker.fetch(item) == FetchFailure("fetch", "OSError")
    assert not worker._slots.locked()
