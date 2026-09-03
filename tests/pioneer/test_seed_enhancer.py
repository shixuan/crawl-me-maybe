"""Seed Enhancer: how many to ask for, what to keep, what to drop."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

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


def _ranker(scores):
    """A ranker that scores the sample it is handed, in order."""
    from crawlme.schemas import RankDecision

    seen: list = []

    async def rank_batch(_goal, cands, _hist, page_contexts=None):
        seen.append(cands)
        return [
            RankDecision(candidate_id=c.candidate_id, priority=scores[i % len(scores)]) for i, c in enumerate(cands)
        ]

    return MagicMock(rank_batch=rank_batch, seen=seen)


async def _verify(proposals, ranker=None, goal=None, **kw):
    from crawlme.pioneer.seed_enhancer import verify

    fetcher, harvester, storage, canon = _rig(**kw)
    kept = await verify(
        proposals,
        fetcher=fetcher,
        harvester=harvester,
        storage=storage,
        canonicalizer=canon,
        ranker=ranker,
        goal=goal,
    )
    return kept, fetcher, harvester


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
    from crawlme.digest.feed.base import PageProblem

    kept, _, _ = await _verify([("https://a.com/", "w")], yields=5, problem=PageProblem.UNAVAILABLE)
    assert kept == []


async def test_an_unreachable_seed_is_dropped():
    kept, _, _ = await _verify([("https://a.com/", "w")], yields=9, fetch_raises=True)
    assert kept == []


async def test_verification_reads_the_page_it_fetched():
    """Payloads go with it: an adapter reading markup alone reports a
    busy account as empty, and a real seed would be thrown away."""
    _, _, harvester = await _verify([("https://a.com/", "w")], yields=3)
    page = harvester.harvest.call_args.args[0]
    assert page.raw_html_path
    assert page.payload_paths == []


async def test_seeds_are_verified_one_by_one():
    kept, fetcher, _ = await _verify([("https://a.com/", "w"), ("https://b.com/", "w")], yields=4)
    assert len(kept) == 2
    assert fetcher.fetch.await_count == 2


# does the ranker want any of it -------------------------------------------


async def test_a_seed_of_no_interest_is_dropped():
    """An events site answers with two hundred links to its own help
    centre. It clears the bar of having something to follow, then spends
    the run going nowhere. Measured: two ext seeds scored zero across
    every candidate they had."""
    kept, _, _ = await _verify([("https://a.com/", "w")], yields=40, ranker=_ranker([0.0]), goal=_goal())
    assert kept == []


async def test_one_wanted_link_keeps_it():
    """The bar is deliberately low. The aggregator that did work scored
    68 of its 181, and a seed worth one page is worth keeping."""
    kept, _, _ = await _verify([("https://a.com/", "w")], yields=40, ranker=_ranker([0.0] * 19 + [0.8]), goal=_goal())
    assert len(kept) == 1


async def test_scoring_costs_one_call():
    """One ranker chunk however many links the page held, so the probe
    does not scale with the size of the page it landed on."""
    from crawlme.pioneer.seed_enhancer import _SAMPLE

    ranker = _ranker([0.9])
    await _verify([("https://a.com/", "w")], yields=200, ranker=ranker, goal=_goal())
    assert len(ranker.seen) == 1
    assert len(ranker.seen[0]) == _SAMPLE


async def test_the_sample_is_not_the_top():
    """The top of a listing is its chrome. On the one aggregator worth
    keeping the first eighteen candidates scored zero, so a head sample
    would have thrown it away."""
    from crawlme.pioneer.seed_enhancer import _SAMPLE

    ranker = _ranker([0.9])
    await _verify([("https://a.com/", "w")], yields=200, ranker=ranker, goal=_goal())
    urls = [c.url.canonical for c in ranker.seen[0]]
    assert urls != [f"https://x.com/{i}" for i in range(_SAMPLE)]
    assert urls[-1] != f"https://x.com/{_SAMPLE - 1}"


async def test_no_ranker_keeps_the_old_bar():
    kept, _, _ = await _verify([("https://a.com/", "w")], yields=5)
    assert len(kept) == 1


async def test_a_ranker_error_keeps_it():
    """A real seed is not worth discarding over a provider hiccup."""
    ranker = MagicMock(rank_batch=AsyncMock(side_effect=RuntimeError("down")))
    kept, _, _ = await _verify([("https://a.com/", "w")], yields=5, ranker=ranker, goal=_goal())
    assert len(kept) == 1


async def test_recall_rejects_still_drop_it():
    """Under --recall a rejection is demoted, not removed, so it lands
    on a small positive score. A gate testing for any score at all
    passes on a page the ranker rejected outright and does nothing."""
    from crawlme.pioneer.ranker import DEMOTED_PRIORITY

    ranker = _ranker([DEMOTED_PRIORITY])
    kept, _, _ = await _verify([("https://a.com/", "w")], yields=40, ranker=ranker, goal=_goal())
    assert kept == []


async def test_enhance_reports_how_many_were_named():
    """None proposed and none surviving are different failures, and a
    run that prints neither looks like a run that was never asked."""
    from crawlme.config import Settings
    from crawlme.pioneer.seed_enhancer import enhance

    fetcher, harvester, storage, canon = _rig(yields=0)
    client = MagicMock()
    client.chat = AsyncMock(
        return_value=LLMResponse(content=_reply(("https://a.com/", "w")), input_tokens=0, output_tokens=0, model="m")
    )
    with patch("crawlme.llm.LLMClient.from_settings_if_configured", return_value=client):
        kept, named = await enhance(
            _goal(),
            ["https://s.com/"],
            settings=Settings(llm_api_key="k"),
            budget=None,
            fetcher=fetcher,
            harvester=harvester,
            storage=storage,
            canonicalizer=canon,
        )
    assert kept == [] and named == 1
