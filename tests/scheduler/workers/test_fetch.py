from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.digest import FetchError, FetchResult
from crawlme.errors import ErrorHandler, Kind
from crawlme.pioneer import FrontierItem
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.scheduler.workers import FetchFailure, FetchWorker
from crawlme.urls import URL


@pytest.mark.asyncio
async def test_fetch_only_returns_response():
    url = URL(raw="https://example.com/p", canonical="https://example.com/p", url_key="p")
    item = FrontierItem(url=url, url_key="p")
    result = FetchResult(item_id="f", url=url, url_key="p", raw=b"body", status_code=200)
    fetcher = MagicMock(fetch=AsyncMock(return_value=result))
    storage = MagicMock()
    worker = FetchWorker(fetcher, RobotsPolicy(ignore=True), storage, concurrency=1, error_handler=ErrorHandler())
    assert await worker.fetch(item) is result
    assert not storage.mock_calls
    fetcher.fetch.side_effect = FetchError("offline", kind=Kind.NOT_FOUND)
    result = await worker.fetch(item)
    assert isinstance(result, FetchFailure) and result.error is fetcher.fetch.side_effect
    assert not worker._slots.locked()


@pytest.mark.asyncio
async def test_fetch_obeys_crawl_delay():
    import time

    url = URL(
        raw="https://news.example.com/p", canonical="https://news.example.com/p", url_key="p", reg_domain="example.com"
    )
    item = FrontierItem(url=url, url_key="p", reg_domain="example.com")
    robots = RobotsPolicy()
    robots.load_robots_txt("news.example.com", "User-agent: *\nAllow: /\nCrawl-delay: 1\n")
    calls = []

    async def fetch(item):
        calls.append(time.monotonic())
        return FetchResult(item_id="f", url=url, url_key="p", status_code=200)

    worker = FetchWorker(MagicMock(fetch=fetch), robots, MagicMock(), concurrency=1, error_handler=ErrorHandler())
    await worker.fetch(item)
    await worker.fetch(item)
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 1


@pytest.mark.parametrize("statuses", [(503, 200), (503, 503), (403, 200)])
async def test_http_recovery_is_owned_by_worker(httpx_mock, statuses):
    from crawlme.digest import HttpFetcher
    from crawlme.errors import ErrorHandler, ErrorPolicy

    url = URL(raw="https://example.com/p", canonical="https://example.com/p", url_key="p")
    item = FrontierItem(url=url, url_key="p")
    for status in statuses:
        httpx_mock.add_response(url=url.canonical, status_code=status, content=b"body")
    worker = FetchWorker(
        HttpFetcher(),
        RobotsPolicy(ignore=True),
        MagicMock(),
        concurrency=1,
        error_handler=ErrorHandler(ErrorPolicy(retry_delay=0)),
        max_attempts=2,
    )
    try:
        result = await worker.fetch(item)
        if statuses[0] == 403:
            assert isinstance(result, FetchFailure)
            assert len(httpx_mock.get_requests()) == 1
            result = await worker.fetch(item)
            assert isinstance(result, FetchResult)
            assert result.fetch_attempt == 1
        elif statuses[-1] == 200:
            assert isinstance(result, FetchResult)
            assert result.fetch_attempt == 2
        else:
            assert isinstance(result, FetchFailure)
            assert result.error.kind == Kind.UNAVAILABLE
        assert len(httpx_mock.get_requests()) == 2
    finally:
        await worker.aclose()
