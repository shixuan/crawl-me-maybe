"""Tests for the LLMRanker with a stub LLM client.

The ranker depends only on the chat(prompt, system=..., ...)
interface, so the client is faked with a scripted responder.
"""

from __future__ import annotations

import datetime

import pytest

from crawlme.errors import Error, Kind
from crawlme.llm import LLMResponse, RemoteError
from crawlme.pioneer import Candidate, RankHistorySummary
from crawlme.pioneer.ranker import LLMRanker, RankingError
from crawlme.prompts import ranking_input
from crawlme.runtime import Goal
from crawlme.urls import URL


class _StubClient:
    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    async def chat(self, prompt: str, *, system: str = "", max_tokens: int | None = None, json_mode: bool = False):
        self.calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens, "json_mode": json_mode})
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, input_tokens=120, output_tokens=40, model="stub")


def _goal() -> Goal:
    return Goal(prompt="find recent rust compiler internals posts")


def _candidates(n: int) -> list[Candidate]:
    return [
        Candidate(
            candidate_id=f"c{i}",
            url=URL(
                raw=f"https://example.com/page/{i}",
                canonical=f"https://example.com/page/{i}",
                url_key=f"k{i}",
                reg_domain="example.com",
            ),
            anchor=f"link {i}",
            snippet=f"snippet {i}",
            depth=1,
        )
        for i in range(n)
    ]


def _candidate(cid: str, **kw) -> Candidate:
    url = f"https://example.com/{cid}"
    return Candidate(
        candidate_id=cid,
        url=URL(raw=url, canonical=url, url_key=cid, reg_domain="example.com"),
        depth=1,
        **kw,
    )


def _rankings_json(n: int, *, drop: list[str] | None = None, priority: float = 0.8) -> str:
    rankings = ", ".join(f'{{"id": "c{i}", "priority": {priority}, "rationale": "because {i}"}}' for i in range(n))
    drops = ", ".join(f'"{d}"' for d in (drop or []))
    return f'{{"rankings": [{rankings}], "candidates_to_drop": [{drops}]}}'


def _ranker(client: _StubClient, batch_size: int = 30, demote_dropped: bool = False) -> LLMRanker:
    return LLMRanker(client, batch_size=batch_size, demote_dropped=demote_dropped)


@pytest.mark.asyncio
async def test_ranks_batch():
    client = _StubClient([_resp(_rankings_json(2))])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())
    assert len(decisions) == 2
    by_id = {d.candidate_id: d for d in decisions}
    assert by_id["c0"].priority == pytest.approx(0.8)
    assert by_id["c0"].dropped is False
    assert by_id["c0"].ranker == "llm"
    assert by_id["c0"].rationale == "because 0"
    assert by_id["c0"].tokens_used == 160
    assert by_id["c1"].url_key == "k1"


@pytest.mark.asyncio
async def test_drop_list():
    client = _StubClient([_resp(_rankings_json(1, drop=["c1"]))])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())
    by_id = {d.candidate_id: d for d in decisions}
    assert by_id["c1"].dropped is True
    assert by_id["c1"].priority == 0.0
    assert by_id["c1"].rationale == "llm_drop"


@pytest.mark.asyncio
async def test_drop_all_junk():
    content = '{"rankings": [], "candidates_to_drop": ["c0", "c1"]}'
    client = _StubClient([_resp(content)])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())
    assert all(d.dropped for d in decisions)


_NEUTRAL = (0.5, False, "no_opinion")


@pytest.mark.parametrize(
    ("content", "n", "expected"),
    [
        # An id in both lists is ranked, not dropped.
        ('{"rankings": [{"id": "c0", "priority": 0.7}], "candidates_to_drop": ["c0"]}', 1, {"c0": (0.7, False, None)}),
        # An id the reply never mentions falls to a neutral keep.
        (_rankings_json(1), 2, {"c1": _NEUTRAL}),
        # Ids that do not exist are ignored, leaving both unmentioned.
        (
            '{"rankings": [{"id": "ghost", "priority": 0.9}], "candidates_to_drop": ["phantom"]}',
            2,
            {"c0": _NEUTRAL, "c1": _NEUTRAL},
        ),
        (
            '{"rankings": [{"id": "c0", "priority": 1.7}, {"id": "c1", "priority": -0.3}], "candidates_to_drop": []}',
            2,
            {"c0": (1.0, False, None), "c1": (0.0, False, None)},
        ),
        # True is not a priority, however happily JSON carries it.
        ('{"rankings": [{"id": "c0", "priority": true}], "candidates_to_drop": []}', 1, {"c0": _NEUTRAL}),
        # Prose around the JSON, and trailing commas inside it.
        ("Here are the scores:\n" + _rankings_json(1) + "\nHope that helps.", 1, {"c0": (0.8, False, "because 0")}),
        (
            '{"rankings": [{"id": "c0", "priority": 0.6,},], "candidates_to_drop": [],}',
            1,
            {"c0": (0.6, False, None)},
        ),
    ],
)
@pytest.mark.asyncio
async def test_reply_lenient(content, n, expected):
    """Whatever the model returns, every candidate comes back decided."""
    decisions = await _ranker(_StubClient([_resp(content)])).rank_batch(_goal(), _candidates(n), RankHistorySummary())
    assert len(decisions) == n
    by_id = {d.candidate_id: d for d in decisions}
    for cid, (priority, dropped, rationale) in expected.items():
        assert by_id[cid].priority == pytest.approx(priority), cid
        assert by_id[cid].dropped is dropped, cid
        if rationale is not None:
            assert by_id[cid].rationale == rationale, cid


@pytest.mark.asyncio
async def test_retry_parses():
    client = _StubClient([_resp("not json at all"), _resp(_rankings_json(2))])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())
    assert len(client.calls) == 2
    assert "was not valid JSON" in client.calls[1]["prompt"]
    assert len(decisions) == 2


@pytest.mark.asyncio
async def test_retry_gives_up():
    client = _StubClient([_resp("garbage"), _resp("still garbage")])
    with pytest.raises(RankingError, match="unparseable JSON") as caught:
        await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())
    assert isinstance(caught.value, Error)
    assert caught.value.source == "ranking" and caught.value.code == "invalid_json"
    assert caught.value.kind == Kind.INVALID_OUTPUT and caught.value.details["candidates"] == 2


@pytest.mark.asyncio
async def test_provider_error():
    client = _StubClient([RemoteError("provider down", kind=Kind.UNAVAILABLE)])
    with pytest.raises(RemoteError):
        await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())


@pytest.mark.asyncio
async def test_chunks_batches():
    client = _StubClient([_resp(_rankings_json(2)), _resp(_rankings_json(2)), _resp(_rankings_json(1))])
    decisions = await _ranker(client, batch_size=2).rank_batch(_goal(), _candidates(5), RankHistorySummary())
    assert len(client.calls) == 3
    assert len(decisions) == 5
    # Each chunk is prompted separately; later chunks start fresh.
    assert "c2" in client.calls[1]["prompt"]
    assert "c4" in client.calls[2]["prompt"]


@pytest.mark.asyncio
async def test_empty_no_call():
    client = _StubClient([])
    decisions = await _ranker(client).rank_batch(_goal(), [], RankHistorySummary())
    assert decisions == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_summary_beats_title():
    """The title is whatever the page put in its head tag. Instagram
    puts the same word there for every page, so the loop carried a
    constant while the summary sat unused in the same record."""
    history = RankHistorySummary(
        relevant_pages=[{"title": "Instagram", "url": "https://x/1", "summary": "Free tote with any purchase"}]
    )
    client = _StubClient([_resp(_rankings_json(1))])
    await _ranker(client).rank_batch(_goal(), _candidates(1), history)
    prompt = client.calls[0]["prompt"]
    assert "Free tote with any purchase" in prompt
    assert "- Instagram" not in prompt


async def test_overrun_splits():
    """A bigger ceiling buys another slow call that runs out too.

    The reply is that long because the batch is that big. One run spent
    four doublings, 33k wasted output tokens and 284 seconds -- half its
    total time -- on the same twenty-one candidates.
    """
    cut_off = LLMResponse(content="{", input_tokens=100, output_tokens=4096, model="stub", truncated=True)
    client = _StubClient([cut_off, _resp(_rankings_json(1)), _resp(_rankings_json(1))])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())

    assert len(client.calls) == 3, "the overrun, then each half"
    assert all(c["max_tokens"] is None for c in client.calls), "the ceiling was never the knob"
    assert len(decisions) == 2, "every candidate still gets a decision"


async def test_single_roomier():
    """There is nothing left to split."""
    cut_off = LLMResponse(content="{", input_tokens=100, output_tokens=4096, model="stub", truncated=True)
    client = _StubClient([cut_off, _resp(_rankings_json(1))])
    await _ranker(client).rank_batch(_goal(), _candidates(1), RankHistorySummary())
    assert client.calls[1]["max_tokens"] == 8192


# recall mode ------------------------------------------------------------


async def test_recall_demotes():
    """A wrong keep is a page you skim; a wrong drop you never learn about.

    The model still says what it doubts, and that still sinks the
    candidate. What changes is who stops the work: the page budget
    rather than one model's yes or no.
    """
    body = '{"rankings": [{"id": "c0", "priority": 0.9}], "candidates_to_drop": ["c1"]}'
    client = _StubClient([_resp(body)])
    decisions = await _ranker(client, demote_dropped=True).rank_batch(_goal(), _candidates(2), RankHistorySummary())

    by_id = {d.candidate_id: d for d in decisions}
    assert by_id["c1"].dropped is False
    assert by_id["c1"].priority < by_id["c0"].priority
    assert by_id["c1"].rationale == "llm_drop_demoted"


async def test_reject_lowest():
    """Silence is weaker evidence than an argument against."""
    body = '{"rankings": [], "candidates_to_drop": ["c1"]}'
    client = _StubClient([_resp(body)])
    decisions = await _ranker(client, demote_dropped=True).rank_batch(_goal(), _candidates(2), RankHistorySummary())

    by_id = {d.candidate_id: d for d in decisions}
    assert by_id["c0"].rationale == "no_opinion"
    assert by_id["c1"].priority < by_id["c0"].priority


async def test_reject_reason():
    """Every misjudged drop was a black box: no reason was ever stored.

    Reading back why the model rejected something is what decides
    whether the fix belongs in the goal the user wrote or in how the
    ranker is asked to judge, instead of guessing between the two.
    """
    body = '{"rankings": [], "candidates_to_drop": [{"id": "c0", "rationale": "a toy shop, not food"}]}'
    client = _StubClient([_resp(body)])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(1), RankHistorySummary())

    assert decisions[0].dropped is True
    assert decisions[0].rationale == "llm_drop: a toy shop, not food"


async def test_demote_reason():
    body = '{"rankings": [], "candidates_to_drop": [{"id": "c0", "rationale": "weaker than the rest"}]}'
    client = _StubClient([_resp(body)])
    decisions = await _ranker(client, demote_dropped=True).rank_batch(_goal(), _candidates(1), RankHistorySummary())
    assert decisions[0].rationale == "llm_drop_demoted: weaker than the rest"
    assert decisions[0].dropped is False


# whole candidates, split batches -----------------------------------------


async def test_candidate_full_text():
    """The line that matters is often the last one.

    A run rejected three real offers because the giveaway sat past
    character 160 of a 489-character post; the model's stated reason was
    that the post contained no giveaway, which was true of what it was
    shown. No cap fixes that, since some post always ends with the point.
    """
    tail = "x" * 900 + " FREE incense chamber with any $15 purchase"
    client = _StubClient([_resp(_rankings_json(1))])
    await _ranker(client).rank_batch(_goal(), [_candidate("c0", text=tail)], RankHistorySummary())
    assert "FREE incense chamber" in client.calls[0]["prompt"]


async def test_split_on_chars():
    """One long post takes room from its batch, not from its own text."""
    client = _StubClient([_resp(_rankings_json(1)), _resp(_rankings_json(1)), _resp(_rankings_json(1))])
    long_ones = [_candidate(f"c{i}", text="y" * 7000) for i in range(3)]
    await _ranker(client).rank_batch(_goal(), long_ones, RankHistorySummary())
    assert len(client.calls) == 3, "three posts too long to share a call"


def _hours_ago(h: float):
    return _utcnow_for_test() - datetime.timedelta(hours=h)


def _utcnow_for_test():
    return datetime.datetime.now(datetime.timezone.utc)


def test_states_age() -> None:
    """Without it a post from an hour ago and one from three years ago
    differ only in their title."""
    c = _candidate("c1", posted_at=_hours_ago(3))
    prompt = ranking_input(Goal(prompt="recent events"), [c], RankHistorySummary(), {})
    assert "posted: 3h ago" in prompt


def test_naive_date_ok() -> None:
    """Subtracting a naive datetime raises, and that would cost the
    other nineteen candidates."""
    c = _candidate("c1", posted_at=datetime.datetime(2026, 1, 1, 12, 0))
    prompt = ranking_input(Goal(prompt="g"), [c], RankHistorySummary(), {})
    assert "posted:" in prompt


def test_window_stated() -> None:
    """The prompt is the user's own words and can disagree with it:
    asked for "this month" with --since "1 week", the model ranked
    three-week-old posts highly and the filter had already dropped
    them."""
    goal = Goal(prompt="public events this month")
    goal.since = datetime.datetime(2026, 8, 20, tzinfo=datetime.timezone.utc)
    prompt = ranking_input(goal, [_candidate("c1")], RankHistorySummary(), {})
    assert "2026-08-20" in prompt


def test_goal_statement_wins() -> None:
    """The analyzer judges the enhanced statement, so the ranker has to
    judge it too, or the two stages score different goals."""
    goal = Goal(prompt="raw words", goal_statement="the enhanced statement")
    prompt = ranking_input(goal, [_candidate("c1")], RankHistorySummary(), {})
    assert "the enhanced statement" in prompt
    assert "raw words" not in prompt


def test_extract_fields_shown() -> None:
    """A candidate that cannot yield the fields is one the analyzer will
    reject, and the ranker could not see them at all."""
    goal = Goal(
        prompt="g",
        extraction_spec={"fields": {"deadline": "when the offer ends"}},
    )
    prompt = ranking_input(goal, [_candidate("c1")], RankHistorySummary(), {})
    assert "## Extract" in prompt
    assert "- deadline: when the offer ends" in prompt
