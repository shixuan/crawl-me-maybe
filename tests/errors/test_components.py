from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.analysis import Analyzer
from crawlme.digest import FetchError, HttpFetcher, Page
from crawlme.errors import Error, ErrorHandler, ErrorPolicy, Kind, Skipped
from crawlme.pioneer import FrontierItem
from crawlme.pioneer.goal_enhancer import GoalEnhancer
from crawlme.pioneer.seed_expander import SeedExpander
from crawlme.runtime import Goal
from crawlme.urls import URL


@pytest.mark.parametrize("component", ["analysis", "goal", "seeds"])
async def test_foreign_error_propagates(component):
    error = Error("database unavailable", source="persist", code="write_failed", kind=Kind.UNAVAILABLE)
    client = MagicMock(chat=AsyncMock(side_effect=error))
    goal = Goal(prompt="compiler articles")
    if component == "analysis":
        analyzer = Analyzer(client)
        page = Page(
            url=URL(raw="https://example.test", canonical="https://example.test", url_key="page"),
            url_key="page",
            plain_text="Compiler article",
        )
        with pytest.raises(Error) as caught:
            await analyzer.analyze(page, goal)
    elif component == "goal":
        with pytest.raises(Error) as caught:
            await GoalEnhancer(client).enhance(goal)
    else:
        with pytest.raises(Error) as caught:
            await SeedExpander(client).propose(goal, ["https://example.test"], 2)
    assert caught.value is error
    assert client.chat.await_count == 1


@pytest.mark.parametrize(
    "status,kind",
    [
        (400, Kind.INVALID_INPUT),
        (401, Kind.UNAUTHENTICATED),
        (403, Kind.FORBIDDEN),
        (404, Kind.NOT_FOUND),
        (408, Kind.TIMEOUT),
        (410, Kind.NOT_FOUND),
        (429, Kind.RATE_LIMITED),
        (503, Kind.UNAVAILABLE),
    ],
)
async def test_fetch_policy_contract(httpx_mock, status, kind):
    address = "https://example.test/article"
    httpx_mock.add_response(url=address, status_code=status, headers={"Retry-After": "0"})
    item = FrontierItem(url=URL(raw=address, canonical=address, url_key=address), url_key=address)
    fetcher = HttpFetcher()
    error_handler = ErrorHandler(ErrorPolicy(max_attempts=1))
    try:
        result = await error_handler.call("fetch", lambda: fetcher.fetch(item), optional=True, repeatable=True)
        assert isinstance(result, Skipped) and isinstance(result.error, FetchError)
        error = result.error
        assert error.source == "fetch" and error.kind == kind
        assert error.details["status_code"] == status
    finally:
        await fetcher.aclose()
