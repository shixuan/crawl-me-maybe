"""Seed Enhancer: how many to ask for, what to keep, what to drop."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.digest.harvest import Harvest
from crawlme.llm import LLMError, LLMResponse
from crawlme.pioneer.seed_enhancer import SeedEnhancer, _parse, how_many
from crawlme.schemas import (
    URL,
    Candidate,
    CrawlGoal,
    FetchResult,
)


def _reply(*pairs) -> str:
    return json.dumps({"seeds": [{"url": u, "why": w} for u, w in pairs]})


def _enhancer(content: str) -> SeedEnhancer:
    client = MagicMock()
    client.chat = AsyncMock(return_value=LLMResponse(content=content, input_tokens=0, output_tokens=0, model="m"))
    return SeedEnhancer(client)


def _goal() -> CrawlGoal:
    return CrawlGoal(prompt="new drinks in Toronto")


# how many ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "want"),
    [(1, 4), (2, 6), (4, 8), (8, 10), (16, 12), (64, 12)],
)
def test_count_grows_slowly(given, want):
    """Doubling the seeds adds two. Past a point the user's own coverage
    is the better guide, and every proposal costs a fetch."""
    assert how_many(given, 4, 12) == want


def test_no_seeds_no_proposals():
    assert how_many(0, 4, 12) == 0


def test_bounds_come_from_settings():
    assert how_many(64, 1, 3) == 3
    assert how_many(1, 7, 12) == 7


# propose -----------------------------------------------------------------


async def test_proposes_what_the_model_named():
    got = await _enhancer(_reply(("https://a.com/", "because"))).propose(_goal(), ["https://s.com/"], 4)
    assert got == [("https://a.com/", "because")]


async def test_a_seed_already_given_is_not_proposed_again():
    """Spending a verification fetch to rediscover the user's own seed."""
    reply = _reply(("https://s.com/", "dup"), ("https://a.com/", "new"))
    got = await _enhancer(reply).propose(_goal(), ["https://s.com/"], 4)
    assert [u for u, _ in got] == ["https://a.com/"]


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
    got = await _enhancer(_reply((spelling, "w"))).propose(_goal(), ["https://www.ig.com/acct/"], 4)
    assert got == []


async def test_two_spellings_of_one_proposal_count_once():
    reply = _reply(("https://a.com/x/", "w"), ("http://www.a.com/x", "w"))
    got = await _enhancer(reply).propose(_goal(), ["https://s.com/"], 4)
    assert len(got) == 1


async def test_more_than_asked_is_cut():
    reply = _reply(*[(f"https://a{i}.com/", "w") for i in range(9)])
    got = await _enhancer(reply).propose(_goal(), ["https://s.com/"], 3)
    assert len(got) == 3


async def test_a_non_url_is_dropped():
    """The model answers in prose sometimes, and a bare handle is not
    something the crawler can fetch."""
    got = await _enhancer(_reply(("chatimecanada", "w"), ("https://a.com/", "w"))).propose(
        _goal(), ["https://s.com/"], 4
    )
    assert [u for u, _ in got] == ["https://a.com/"]


async def test_no_client_proposes_nothing():
    """Inert without credentials, like the Goal Enhancer."""
    assert await SeedEnhancer(None).propose(_goal(), ["https://s.com/"], 4) == []


async def test_an_llm_error_proposes_nothing():
    client = MagicMock()
    client.chat = AsyncMock(side_effect=LLMError("down"))
    assert await SeedEnhancer(client).propose(_goal(), ["https://s.com/"], 4) == []


def test_unparseable_replies_are_dropped():
    for content in ("", "no json here", "{not json}", '{"seeds": "not a list"}'):
        assert _parse(content, known=set(), want=4) == []


def test_prose_around_the_json_is_tolerated():
    content = 'Here you go:\n{"seeds": [{"url": "https://a.com/", "why": "w"}]}\nHope that helps.'
    assert _parse(content, known=set(), want=4) == [("https://a.com/", "w")]


def test_a_long_reason_is_cut():
    """It is shown back to a person deciding whether to keep the seed,
    so it has to fit on the line it is printed on."""
    from crawlme.pioneer.seed_enhancer import _MAX_WHY

    content = json.dumps({"seeds": [{"url": "https://a.com/", "why": "x" * 500}]})
    assert len(_parse(content, known=set(), want=4)[0][1]) == _MAX_WHY


# verify ------------------------------------------------------------------


def _url(u: str) -> URL:
    return URL(raw=u, canonical=u, url_key=u, reg_domain="x.com")


def _rig(*, yields: int, problem=None, fetch_raises=False):
    """A fetcher and harvester that answer however the case needs."""
    fetcher = MagicMock()
    if fetch_raises:
        fetcher.fetch = AsyncMock(side_effect=OSError("gone"))
    else:
        fetcher.fetch = AsyncMock(
            side_effect=lambda it: FetchResult(item_id="f1", url=it.url, url_key=it.url_key, status=200, raw=b"<html/>")
        )
    harvester = MagicMock()
    harvester.harvest = MagicMock(
        return_value=Harvest([Candidate(url=_url(f"https://x.com/{i}")) for i in range(yields)], problem)
    )
    storage = MagicMock()
    storage.save_raw_html = MagicMock(return_value="raw.html")
    storage.save_payload = MagicMock(return_value="p.0")
    canon = MagicMock()
    canon.canonicalize = MagicMock(side_effect=lambda raw, _b: _url(raw))
    return fetcher, harvester, storage, canon


async def _verify(proposals, **kw):
    from crawlme.pioneer.seed_enhancer import verify

    fetcher, harvester, storage, canon = _rig(**kw)
    kept, dropped = await verify(
        proposals,
        fetcher=fetcher,
        harvester=harvester,
        storage=storage,
        canonicalizer=canon,
    )
    return kept, fetcher, harvester, dropped


async def test_a_seed_that_yields_is_kept():
    kept, _, _, _ = await _verify([("https://a.com/", "why")], yields=18)
    assert [c.url.canonical for c in kept] == ["https://a.com/"]
    assert kept[0].seed_ext is True
    assert kept[0].signals["why"] == "why"


async def test_a_seed_that_yields_nothing_is_dropped():
    """An invented account answers 200 and renders. Only reading it
    apart from a real one, which is what the harvester does."""
    kept, _, _, _ = await _verify([("https://a.com/", "w")], yields=0)
    assert kept == []


async def test_a_refusal_is_dropped():
    from crawlme.digest.feed.base import PageProblem

    kept, _, _, _ = await _verify([("https://a.com/", "w")], yields=5, problem=PageProblem.UNAVAILABLE)
    assert kept == []


async def test_an_unreachable_seed_is_dropped():
    kept, _, _, _ = await _verify([("https://a.com/", "w")], yields=9, fetch_raises=True)
    assert kept == []


async def test_verification_reads_the_page_it_fetched():
    """Payloads go with it: an adapter reading markup alone reports a
    busy account as empty, and a real seed would be thrown away."""
    _, _, harvester, _ = await _verify([("https://a.com/", "w")], yields=3)
    page = harvester.harvest.call_args.args[0]
    assert page.raw_html_path
    assert page.payload_paths == []


async def test_seeds_are_verified_one_by_one():
    kept, fetcher, _, _ = await _verify([("https://a.com/", "w"), ("https://b.com/", "w")], yields=4)
    assert len(kept) == 2
    assert fetcher.fetch.await_count == 2


async def test_a_rejection_says_which_kind_it_was():
    """An address the model guessed and one that would not load cost the
    same fetch and mean different things."""
    from crawlme.digest.feed.base import PageProblem

    _, _, _, gone = await _verify([("https://a.com/", "w")], yields=5, problem=PageProblem.UNAVAILABLE)
    assert gone == [("https://a.com/", "does not exist")]

    _, _, _, unreachable = await _verify([("https://b.com/", "w")], yields=5, fetch_raises=True)
    assert unreachable == [("https://b.com/", "could not be fetched")]

    _, _, _, kept_none = await _verify([("https://c.com/", "w")], yields=4)
    assert kept_none == []
