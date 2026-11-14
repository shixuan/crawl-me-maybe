"""Seed Expander: how many to ask for, what to keep, what to drop."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.discovery import Harvest
from crawlme.errors import Kind
from crawlme.llm import LLMResponse, RemoteError
from crawlme.pioneer import Candidate
from crawlme.pioneer.seed_expander import SeedExpander, _parse
from crawlme.runtime import Goal
from crawlme.urls import URL


def _reply(*pairs) -> str:
    return json.dumps({"seeds": [{"url": u, "why": w} for u, w in pairs]})


def _expander(content: str) -> SeedExpander:
    client = MagicMock()
    client.chat = AsyncMock(return_value=LLMResponse(content=content, input_tokens=0, output_tokens=0, model="m"))
    return SeedExpander(client)


def _goal() -> Goal:
    return Goal(prompt="new drinks in Toronto")


# propose -----------------------------------------------------------------


async def test_proposes_what_the_model_named():
    got = await _expander(_reply(("https://a.com/", "because"))).propose(_goal(), ["https://s.com/"], 4)
    assert got == [("https://a.com/", "because")]


@pytest.mark.parametrize(
    "spelling",
    [
        "https://www.ig.com/acct/",
        "https://www.ig.com/acct",
        "https://ig.com/acct/",
        "http://www.ig.com/acct/",
        "https://www.IG.com/Acct/",
    ],
)
async def test_a_seed_respelled_is_still_the_same_seed(spelling):
    """Asked twice, a model writes one account several ways. Each costs
    a fetch and then hands back a seed the user already gave."""
    got = await _expander(_reply((spelling, "w"))).propose(_goal(), ["https://www.ig.com/acct/"], 4)
    assert got == []


async def test_two_spellings_of_one_proposal_count_once():
    reply = _reply(("https://a.com/x/", "w"), ("http://www.a.com/x", "w"))
    got = await _expander(reply).propose(_goal(), ["https://s.com/"], 4)
    assert len(got) == 1


async def test_more_than_asked_is_cut():
    reply = _reply(*[(f"https://a{i}.com/", "w") for i in range(9)])
    got = await _expander(reply).propose(_goal(), ["https://s.com/"], 3)
    assert len(got) == 3


async def test_a_non_url_is_dropped():
    """The model answers in prose sometimes, and a bare handle is not
    something the crawler can fetch."""
    got = await _expander(_reply(("chatimecanada", "w"), ("https://a.com/", "w"))).propose(
        _goal(), ["https://s.com/"], 4
    )
    assert [u for u, _ in got] == ["https://a.com/"]


async def test_no_client_proposes_nothing():
    """Inert without credentials, like the Goal Enhancer."""
    assert await SeedExpander(None).propose(_goal(), ["https://s.com/"], 4) == []


async def test_llm_error_propagates():
    client = MagicMock()
    client.chat = AsyncMock(side_effect=RemoteError("down", kind=Kind.UNAVAILABLE))
    with pytest.raises(RemoteError):
        await SeedExpander(client).propose(_goal(), ["https://s.com/"], 4)


def test_unparseable_replies_are_dropped():
    for content in ("", "no json here", "{not json}", '{"seeds": "not a list"}'):
        assert _parse(content, known=set(), want=4) == []


def test_prose_around_the_json_is_tolerated():
    content = 'Here you go:\n{"seeds": [{"url": "https://a.com/", "why": "w"}]}\nHope that helps.'
    assert _parse(content, known=set(), want=4) == [("https://a.com/", "w")]


# verify ------------------------------------------------------------------


def _url(u: str) -> URL:
    return URL(raw=u, canonical=u, url_key=u, reg_domain="x.com")


def _rig(*, yields: int, problem=None, fetch_raises=False):
    """The scheduler's probe supplies discovered candidates or a fetch error."""
    if fetch_raises:
        from crawlme.digest import FetchError

        probe = AsyncMock(side_effect=FetchError("gone"))
    else:
        probe = AsyncMock(
            return_value=Harvest([Candidate(url=_url(f"https://x.com/{i}")) for i in range(yields)], problem)
        )
    canon = MagicMock()
    canon.canonicalize = MagicMock(side_effect=lambda raw, _b: _url(raw))
    return probe, canon


async def _verify(proposals, **kw):
    from crawlme.pioneer.seed_expander import verify

    probe, canon = _rig(**kw)
    kept, dropped = await verify(
        proposals,
        probe=probe,
        canonicalizer=canon,
    )
    return kept, probe, dropped


async def test_a_seed_that_yields_is_kept():
    kept, _, _ = await _verify([("https://a.com/", "why")], yields=18)
    assert [c.url.canonical for c in kept] == ["https://a.com/"]
    assert kept[0].seed_ext is True
    assert kept[0].signals["why"] == "why"


async def test_a_seed_that_yields_nothing_is_dropped():
    """An invented account answers 200 and renders. Only reading it
    apart from a real one, which is what the harvester does."""
    kept, _, _ = await _verify([("https://a.com/", "w")], yields=0)
    assert kept == []


async def test_a_refusal_is_dropped():
    from crawlme.platforms import PageProblem

    kept, _, _ = await _verify([("https://a.com/", "w")], yields=5, problem=PageProblem.UNAVAILABLE)
    assert kept == []


async def test_an_unreachable_seed_is_dropped():
    kept, _, _ = await _verify([("https://a.com/", "w")], yields=9, fetch_raises=True)
    assert kept == []


async def test_a_rejection_says_which_kind_it_was():
    """An address the model guessed and one that would not load cost the
    same fetch and mean different things."""
    from crawlme.platforms import PageProblem

    _, _, gone = await _verify([("https://a.com/", "w")], yields=5, problem=PageProblem.UNAVAILABLE)
    assert gone == [("https://a.com/", "does not exist")]

    _, _, unreachable = await _verify([("https://b.com/", "w")], yields=5, fetch_raises=True)
    assert unreachable == [("https://b.com/", "gone")]

    _, _, kept_none = await _verify([("https://c.com/", "w")], yields=4)
    assert kept_none == []


async def test_verification_preserves_unexpected_failures():
    from crawlme.pioneer.seed_expander import verify

    probe, canonicalizer = _rig(yields=0)
    failure = OSError("storage unavailable")
    probe.side_effect = failure
    with pytest.raises(OSError) as caught:
        await verify([("https://a.com/", "why")], probe=probe, canonicalizer=canonicalizer)
    assert caught.value is failure


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
    expander = _expander('{"queries": ["Toronto gifts", "Toronto gifts"]}')
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
    expander = _expander('{"queries": ["gifts"]}')
    expander._adapters = (search,)
    expander._fetcher_factory = lambda: AsyncMock()
    assert await expander.search(_goal(), ["https://www.instagram.com/shop/"], 4) == []
    expander._client.chat.assert_not_awaited()


@pytest.mark.parametrize("reply", ["{}", '{"queries": "bad"}', "not json", '{"queries": [null, 3, ""]}'])
async def test_invalid_search_queries_allow_fallback(reply):
    search = MagicMock(claims_url=MagicMock(return_value=True), PLATFORM="instagram")
    expander = _expander(reply)
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
    expander = _expander('{"queries": ["gifts", "freebies"]}')
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


def test_missing_session_excludes_instagram_search(tmp_path):
    from crawlme.config import Settings

    expander = SeedExpander.from_settings(Settings(browser_storage_state=str(tmp_path / "missing.json")))
    assert "instagram" not in [a.PLATFORM for a in expander._adapters]
    assert "reddit" in [a.PLATFORM for a in expander._adapters]


async def test_search_refusal_skips_parsing_and_closes_fetcher():
    from crawlme.platforms import reddit

    fetcher = AsyncMock()
    fetcher.fetch.return_value = SimpleNamespace(raw=b"You've been blocked by network security.", payloads=[])
    adapter = MagicMock(wraps=reddit)
    adapter.PLATFORM = reddit.PLATFORM
    expander = _expander('{"queries": ["gifts", "freebies"]}')
    expander._adapters = (adapter,)
    expander._fetcher_factory = lambda: fetcher
    assert await expander.search(_goal(), ["https://www.reddit.com/r/test/"], 4) == []
    adapter.parse_search.assert_not_called()
    fetcher.fetch.assert_awaited_once()
    fetcher.aclose.assert_awaited_once()


async def test_expand_fallback_reports_rejected_sources():
    from crawlme.urls import Canonicalizer

    expander = SeedExpander(None)
    expander.search = AsyncMock(return_value=[])
    expander.propose = AsyncMock(return_value=[("https://example.com/empty", "source")])
    probe = AsyncMock(return_value=Harvest([]))
    result = await expander.expand(_goal(), ["https://example.com/"], 4, probe=probe, canonicalizer=Canonicalizer())
    assert result.candidates == []
    assert result.asked == 1
    assert result.rejected == [("https://example.com/empty", "held nothing to follow")]
    probe.assert_awaited_once()


async def test_expand_cancellation_does_not_fall_back():
    import asyncio

    from crawlme.urls import Canonicalizer

    expander = SeedExpander(None)
    expander.search = AsyncMock(side_effect=asyncio.CancelledError())
    expander.propose = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await expander.expand(_goal(), ["https://example.com/"], 4, probe=AsyncMock(), canonicalizer=Canonicalizer())
    expander.propose.assert_not_awaited()


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
    from crawlme.pioneer.seed_expander import _allocate

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
    expander = _expander('{"queries": ["first", "second", "third"]}')
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
    expander = _expander('{"queries": ["query"]}')
    expander._adapters = tuple(adapters)
    expander._platforms = ("reddit",)
    expander._fetcher_factory = lambda: fetcher

    result = await expander.search(_goal(), seeds, 4)

    assert len(result) == 1
    assert result[0].signals["why"] == f"{expected} search: query"
    for adapter in adapters:
        assert adapter.parse_search.call_count == int(adapter.PLATFORM == expected)


async def test_seedless_proposals_stay_on_requested_platform():
    expander = _expander(_reply(("https://www.reddit.com/r/toronto/", "local"), ("https://example.com/", "other")))
    expander._platforms = ("reddit",)
    result = await expander.propose(_goal(), [], 4)
    assert result == [("https://www.reddit.com/r/toronto/", "local")]
    assert "Only propose sources on these platforms\nreddit" in expander._client.chat.call_args.args[0]
