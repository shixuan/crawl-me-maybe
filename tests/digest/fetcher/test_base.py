"""with_retries: the one behaviour both fetchers share.

Extracted as a function rather than a base class, so what it promises has
to hold for callers that classify failures completely differently: httpx
raises its own exceptions, a browser raises Playwright's.
"""

from __future__ import annotations

import asyncio

import pytest

from crawlme.digest.fetcher import FetchError, with_retries
from crawlme.schemas import URL, FetchResult


def _result() -> FetchResult:
    url = URL(raw="https://x.com/a", canonical="https://x.com/a", url_key="k1")
    return FetchResult(item_id="i1", url_key="k1", url=url, status_code=200)


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """Backoff is real seconds; the schedule is tested, not endured.

    The original has to be captured first: patching asyncio.sleep with a
    lambda that calls asyncio.sleep calls the patch.
    """
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _d: real_sleep(0))


@pytest.mark.asyncio
async def test_first_try_ok():
    calls = []

    async def attempt(n):
        calls.append(n)
        return _result()

    assert (await with_retries(attempt, max_retries=3, is_transient=lambda e: True)).status_code == 200
    assert calls == [1]


@pytest.mark.asyncio
async def test_transient_retry():
    calls = []

    async def attempt(n):
        calls.append(n)
        if n < 3:
            raise TimeoutError("slow")
        return _result()

    await with_retries(attempt, max_retries=3, is_transient=lambda e: isinstance(e, TimeoutError))
    assert calls == [1, 2, 3]


@pytest.mark.asyncio
async def test_retries_spent():
    async def attempt(n):
        raise TimeoutError("always slow")

    with pytest.raises(FetchError, match="after 3 attempts"):
        await with_retries(attempt, max_retries=3, is_transient=lambda e: True)


@pytest.mark.asyncio
async def test_permanent_not_retried():
    """A 404 does not become a 200 by asking again."""
    calls = []

    async def attempt(n):
        calls.append(n)
        raise FetchError("gone")

    with pytest.raises(FetchError, match="gone"):
        await with_retries(attempt, max_retries=3, is_transient=lambda e: True)
    assert calls == [1]


@pytest.mark.asyncio
async def test_caller_decides():
    """Each fetcher decides what transient means in its own vocabulary."""
    calls = []

    async def attempt(n):
        calls.append(n)
        raise ValueError("not our kind of failure")

    with pytest.raises(ValueError):
        await with_retries(attempt, max_retries=3, is_transient=lambda e: isinstance(e, TimeoutError))
    assert calls == [1], "an unclassified failure must surface immediately"


@pytest.mark.asyncio
async def test_cause_kept():
    async def attempt(n):
        raise TimeoutError("the real reason")

    with pytest.raises(FetchError) as caught:
        await with_retries(attempt, max_retries=2, is_transient=lambda e: True)
    assert isinstance(caught.value.__cause__, TimeoutError)


@pytest.mark.asyncio
async def test_single_attempt():
    calls = []

    async def attempt(n):
        calls.append(n)
        return _result()

    await with_retries(attempt, max_retries=1, is_transient=lambda e: True)
    assert calls == [1]
