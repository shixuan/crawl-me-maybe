"""Platform selection, search filtering and allocation."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crawlme.goals import Goal
from crawlme.llm import LLMResponse
from crawlme.scheduler.pipelines.factory import _build_seed_expander
from crawlme.seeds import SeedSearch


def _search(content: str) -> SeedSearch:
    client = MagicMock()
    client.chat = AsyncMock(return_value=LLMResponse(content=content, input_tokens=0, output_tokens=0, model="m"))
    return SeedSearch(client)


def _goal() -> Goal:
    return Goal(prompt="new drinks in Toronto")


async def test_search_filters_old_undated_and_known_posts():
    import datetime

    from crawlme.platforms.models import FeedItem

    since = datetime.datetime(2026, 11, 2, tzinfo=datetime.timezone.utc)
    recent = since + datetime.timedelta(days=1)
    posts = [
        FeedItem(
            "https://www.instagram.com/shop/p/OLD/",
            "instagram",
            text="Old",
            published_at=since - datetime.timedelta(days=365),
        ),
        FeedItem("https://www.instagram.com/shop/p/UNKNOWN/", "instagram", text="Unknown"),
        FeedItem("https://www.instagram.com/shop/p/KNOWN/", "instagram", text="Known", published_at=recent),
        FeedItem("https://www.instagram.com/shop/p/NEW/", "instagram", text="New", published_at=recent),
    ]
    search = MagicMock(
        claims_url=MagicMock(return_value=True),
        PLATFORM="instagram",
        search_url=MagicMock(return_value="https://www.instagram.com/explore/search/keyword/?q=gifts"),
        problem=MagicMock(return_value=None),
        parse_search=MagicMock(return_value=posts + posts),
        seed_contains=MagicMock(return_value=False),
    )
    expander = _search('{"queries": ["Toronto gifts", "Toronto gifts"]}')
    expander._adapters = (search,)
    fetcher = AsyncMock()
    fetcher.fetch.return_value = SimpleNamespace(raw=b"<html></html>", payloads=[])
    expander._fetcher_factory = lambda: fetcher
    result = await expander.search(Goal(prompt="gifts", since=since), [posts[2].permalink], 4)
    assert [c.url.raw for c in result] == [posts[3].permalink]
    assert result[0].posted_at == recent
    assert result[0].seed_ext
    search.search_url.assert_called_once_with("Toronto gifts")
    search.parse_search.assert_called_once()
    fetcher.aclose.assert_awaited_once()


async def test_search_unavailable_does_not_spend_tokens():
    search = MagicMock(claims_url=MagicMock(return_value=False), PLATFORM="instagram")
    expander = _search('{"queries": ["gifts"]}')
    expander._adapters = (search,)
    expander._fetcher_factory = lambda: AsyncMock()
    assert await expander.search(_goal(), ["https://www.instagram.com/shop/"], 4) == []
    expander._client.chat.assert_not_awaited()


@pytest.mark.parametrize("reply", ["{}", '{"queries": "bad"}', "not json", '{"queries": [null, 3, ""]}'])
async def test_invalid_search_queries_allow_fallback(reply):
    search = MagicMock(claims_url=MagicMock(return_value=True), PLATFORM="instagram")
    expander = _search(reply)
    expander._adapters = (search,)
    expander._fetcher_factory = lambda: AsyncMock()
    assert await expander.search(_goal(), ["https://www.instagram.com/shop/"], 4) == []
    search.search_url.assert_not_called()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_search_failure_cleanup(cancelled):
    import asyncio

    failure = asyncio.CancelledError() if cancelled else OSError("browser unavailable")
    adapter = MagicMock(
        PLATFORM="example",
        claims_url=MagicMock(return_value=True),
        search_url=MagicMock(return_value="https://example.com/search?q=gifts"),
    )
    fetcher = AsyncMock()
    fetcher.fetch.side_effect = failure
    expander = _search('{"queries": ["gifts", "freebies"]}')
    expander._adapters = (adapter,)
    expander._fetcher_factory = lambda: fetcher
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await expander.search(_goal(), ["https://example.com"], 4)
    else:
        assert await expander.search(_goal(), ["https://example.com"], 4) == []
    adapter.search_url.assert_called_once_with("gifts")
    fetcher.fetch.assert_awaited_once()
    fetcher.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    "seeds, platforms, warns",
    [
        (["https://www.reddit.com/r/toronto/"], ["instagram"], False),
        ([], ["reddit"], False),
        ([], ["instagram"], True),
        (["https://www.instagram.com/shop/"], ["reddit"], True),
        (["https://www.reddit.com/r/toronto/", "https://www.instagram.com/shop/"], [], True),
    ],
)
async def test_missing_session_warns_only_for_selected_platforms(tmp_path, caplog, seeds, platforms, warns):
    from crawlme.config import Settings
    from crawlme.llm import Client

    client = MagicMock()
    client.chat = AsyncMock(return_value=SimpleNamespace(content='{"queries": []}', truncated=False))
    with patch.object(Client, "from_settings_if_configured", return_value=client):
        expander = _build_seed_expander(Settings(search_platforms=platforms), storage_state=None)._search
    assert not caplog.records
    await expander.search(_goal(), seeds, 4)
    warnings = [record.getMessage() for record in caplog.records if "no session file found" in record.getMessage()]
    assert len(warnings) == int(warns)
    if warns:
        assert "crawl session ./instagram-session.json --feed instagram" in warnings[0]
    if not seeds and platforms == ["instagram"]:
        client.chat.assert_not_awaited()


async def test_search_refusal_skips_parsing_and_closes_fetcher():
    from crawlme.platforms import reddit

    fetcher = AsyncMock()
    fetcher.fetch.return_value = SimpleNamespace(raw=b"You've been blocked by network security.", payloads=[])
    adapter = MagicMock(wraps=reddit)
    adapter.PLATFORM = reddit.PLATFORM
    expander = _search('{"queries": ["gifts", "freebies"]}')
    expander._adapters = (adapter,)
    expander._fetcher_factory = lambda: fetcher
    assert await expander.search(_goal(), ["https://www.reddit.com/r/test/"], 4) == []
    adapter.parse_search.assert_not_called()
    fetcher.fetch.assert_awaited_once()
    fetcher.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    ("counts", "limit", "expected"),
    [
        ([19, 11, 39], 11, [3, 2, 6]),
        ([0, 3, 1], 10, [0, 3, 1]),
        ([0, 0], 4, [0, 0]),
        ([1, 1, 1], 2, [1, 1, 0]),
        ([5, 5], 0, [0, 0]),
    ],
)
def test_weighted_search_allocation(counts, limit, expected):
    from crawlme.seeds.search import _allocate

    assert _allocate(counts, limit) == expected


async def test_search_allocates_after_excluding_seed_accounts_and_duplicates():
    from crawlme.platforms import instagram
    from crawlme.platforms.models import FeedItem

    def post(code, author="other"):
        return FeedItem(f"https://www.instagram.com/{author}/p/{code}/", "instagram", author=author, text=code)

    shared = post("shared")
    groups = [
        [post("covered", "torontodiscounts"), shared, post("a")],
        [shared, post("b")],
        [post("c"), post("d"), post("e")],
    ]
    adapter = MagicMock(wraps=instagram)
    adapter.PLATFORM = "instagram"
    adapter.parse_search.side_effect = groups
    fetcher = AsyncMock()
    fetcher.fetch.return_value = SimpleNamespace(raw=b"<html></html>", payloads=[])
    expander = _search('{"queries": ["first", "second", "third"]}')
    expander._adapters = (adapter,)
    expander._fetcher_factory = lambda: fetcher
    result = await expander.search(_goal(), ["https://www.instagram.com/torontodiscounts/"], 4)
    assert [c.signals["why"] for c in result] == [
        "instagram search: first",
        "instagram search: second",
        "instagram search: third",
        "instagram search: third",
    ]
    assert len({c.url.url_key for c in result}) == 4
    assert all(c.signals["account"] != "torontodiscounts" for c in result)


@pytest.mark.parametrize(
    "seeds, expected",
    [([], "reddit"), (["https://www.instagram.com/example/"], "instagram")],
)
async def test_search_uses_platform_only_without_seeds(seeds, expected):
    from crawlme.platforms import instagram, reddit
    from crawlme.platforms.models import FeedItem

    adapters = []
    for platform in (instagram, reddit):
        adapter = MagicMock(wraps=platform)
        adapter.PLATFORM = platform.PLATFORM
        adapter.parse_search.return_value = [FeedItem("https://example.com/post", platform.PLATFORM, text="a post")]
        adapters.append(adapter)
    fetcher = AsyncMock()
    fetcher.fetch.return_value = SimpleNamespace(raw=b"<html></html>", payloads=[])
    expander = _search('{"queries": ["query"]}')
    expander._adapters = tuple(adapters)
    expander._platforms = ("reddit",)
    expander._fetcher_factory = lambda: fetcher

    result = await expander.search(_goal(), seeds, 4)

    assert len(result) == 1
    assert result[0].signals["why"] == f"{expected} search: query"
    for adapter in adapters:
        assert adapter.parse_search.call_count == int(adapter.PLATFORM == expected)


@pytest.mark.parametrize("count", [1, 5])
async def test_search_count_controls_prompt_and_requests(count):
    from crawlme.config import Settings
    from crawlme.llm import Client

    client = MagicMock()
    client.chat = AsyncMock(
        return_value=SimpleNamespace(content=json.dumps({"queries": [f"query {i}" for i in range(6)]}), truncated=False)
    )
    with patch.object(Client, "from_settings_if_configured", return_value=client):
        expander = _build_seed_expander(Settings(expand_search_count=count), storage_state=None)._search
    adapter = MagicMock(
        PLATFORM="reddit",
        NEEDS_SESSION=False,
        claims_url=MagicMock(return_value=True),
        search_url=MagicMock(return_value="https://www.reddit.com/search/?q=test"),
        problem=MagicMock(return_value=None),
        parse_search=MagicMock(return_value=[]),
    )
    fetcher = AsyncMock()
    fetcher.fetch.return_value = SimpleNamespace(raw=b"<html></html>", payloads=[])
    expander._adapters = (adapter,)
    expander._fetcher_factory = lambda: fetcher

    await expander.search(_goal(), ["https://www.reddit.com/r/toronto/"], 12)

    assert f"Generate at most {count} queries." in client.chat.call_args.kwargs["system"]
    assert [call.args[0] for call in adapter.search_url.call_args_list] == [f"query {i}" for i in range(count)]
    assert fetcher.fetch.await_count == count
