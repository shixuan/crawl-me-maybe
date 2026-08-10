from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from crawlme.digest.fetcher import Fetcher, FetchError
from crawlme.schemas import URL, FrontierItem


def _item(url_str: str = "https://example.com/page") -> FrontierItem:
    return FrontierItem(
        url=URL(raw=url_str, canonical=url_str, url_key="k1", reg_domain="example.com"),
        url_key="k1",
    )


@pytest.fixture
def fetcher() -> Fetcher:
    return Fetcher(max_retries=2)


@pytest.mark.asyncio
async def test_success(fetcher, httpx_mock: HTTPXMock):
    httpx_mock.add_response(url="https://example.com/page", content=b"<html>ok</html>")
    result = await fetcher.fetch(_item())
    assert result.status_code == 200
    assert result.raw == b"<html>ok</html>"


@pytest.mark.asyncio
async def test_records_redirect_chain(fetcher, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://example.com/page",
        status_code=301,
        headers={"Location": "https://example.com/new"},
    )
    httpx_mock.add_response(
        url="https://example.com/new",
        content=b"<html>moved</html>",
    )
    result = await fetcher.fetch(_item())
    assert result.status_code == 200
    assert len(result.redirects) > 0


@pytest.mark.asyncio
async def test_retry_on_5xx(fetcher, httpx_mock: HTTPXMock):
    httpx_mock.add_response(url="https://example.com/page", status_code=503, is_reusable=True)
    httpx_mock.add_response(url="https://example.com/page", content=b"recovered")
    result = await fetcher.fetch(_item())
    assert result.status_code == 200
    assert result.fetch_attempt == 2


@pytest.mark.asyncio
async def test_raises_after_max_retries(fetcher, httpx_mock: HTTPXMock):
    httpx_mock.add_response(url="https://example.com/page", status_code=503, is_reusable=True)

    with pytest.raises(FetchError):
        await fetcher.fetch(_item())


@pytest.mark.asyncio
async def test_permanent_4xx_no_retry(fetcher, httpx_mock: HTTPXMock):
    httpx_mock.add_response(url="https://example.com/page", status_code=404)

    with pytest.raises(FetchError):
        await fetcher.fetch(_item())


@pytest.mark.asyncio
async def test_429_triggers_delay(fetcher, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://example.com/page",
        status_code=429,
        headers={"Retry-After": "0"},
        is_reusable=True,
    )
    httpx_mock.add_response(url="https://example.com/page", content=b"ok")
    result = await fetcher.fetch(_item())
    assert result.status_code == 200
