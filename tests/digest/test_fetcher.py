from __future__ import annotations

import asyncio
import time

import pytest
from pytest_httpx import HTTPXMock

from crawlme.digest.fetcher import FetchError, HttpFetcher
from crawlme.schemas import URL, FetchResult, FrontierItem


def _item(url_str: str = "https://example.com/page") -> FrontierItem:
    return FrontierItem(
        url=URL(raw=url_str, canonical=url_str, url_key="k1", reg_domain="example.com"),
        url_key="k1",
    )


@pytest.fixture
def fetcher() -> HttpFetcher:
    return HttpFetcher(max_retries=2)


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


# -- total deadline ----------------------------------------------------


@pytest.mark.asyncio
async def test_total_timeout_caps_hung_fetch():
    """A trickle-feed host that never finishes is cut off by the deadline.

    Per-phase timeouts can't catch this: bytes arriving every few
    seconds reset the read timer forever.
    """
    fetcher = HttpFetcher(max_retries=1, total_timeout=0.2)
    t0 = time.monotonic()

    with pytest.raises(FetchError):
        await fetcher.fetch(_item())

    assert time.monotonic() - t0 < 2.0  # cut short, not hung


@pytest.mark.asyncio
async def test_total_timeout_retries_then_succeeds(monkeypatch):
    """A timed-out attempt is transient: the next attempt may win."""
    calls: list[int] = []

    async def _flaky(self, item, attempt, started):
        calls.append(attempt)
        if attempt == 1:
            await asyncio.sleep(3600)  # hang: only the deadline can stop it
        return FetchResult(
            item_id=item.item_id,
            url_key=item.url_key,
            url=item.url,
            status_code=200,
            raw=b"ok",
        )

    monkeypatch.setattr(HttpFetcher, "_do_fetch", _flaky)
    fetcher = HttpFetcher(max_retries=2, total_timeout=0.1)

    result = await fetcher.fetch(_item())
    assert result.status_code == 200
    assert calls == [1, 2]


# -- redirect caps -----------------------------------------------------


@pytest.mark.asyncio
async def test_redirect_loop_detected(fetcher, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://example.com/page",
        status_code=302,
        headers={"Location": "https://example.com/other"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        url="https://example.com/other",
        status_code=302,
        headers={"Location": "https://example.com/page"},
        is_reusable=True,
    )

    with pytest.raises(FetchError, match="loop"):
        await fetcher.fetch(_item())


@pytest.mark.asyncio
async def test_too_many_redirects(fetcher, httpx_mock: HTTPXMock):
    # 11 hops: page -> r0 -> ... -> r9.  The 10th follow is rejected
    # before the next request fires, so register exactly what is fetched.
    httpx_mock.add_response(
        url="https://example.com/page",
        status_code=302,
        headers={"Location": "https://example.com/r0"},
    )
    for i in range(10):
        httpx_mock.add_response(
            url=f"https://example.com/r{i}",
            status_code=302,
            headers={"Location": f"https://example.com/r{i + 1}"},
        )

    with pytest.raises(FetchError, match="redirects"):
        await fetcher.fetch(_item())


@pytest.mark.asyncio
async def test_fetches_canonical_url_for_relative_href(fetcher, httpx_mock: HTTPXMock):
    """A site-relative href (raw) must be requested through its resolved
    canonical URL.  Fetching raw directly is what produced a wall of
    UnsupportedProtocol errors on HN-style relative links."""
    item = FrontierItem(
        url=URL(
            raw="from?site=blog.google",
            canonical="https://news.ycombinator.com/from?site=blog.google",
            url_key="k-rel",
            reg_domain="news.ycombinator.com",
        ),
        url_key="k-rel",
    )
    httpx_mock.add_response(url="https://news.ycombinator.com/from?site=blog.google", content=b"ok")
    result = await fetcher.fetch(item)
    assert result.status_code == 200
