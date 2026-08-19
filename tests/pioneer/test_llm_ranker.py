"""Tests for the LLMRanker with a stub LLM client.

The ranker depends only on the chat(prompt, system=..., ...)
interface, so the client is faked with a scripted responder.
"""

from __future__ import annotations

import pytest

from crawlme.config import Settings
from crawlme.llm import LLMError, LLMResponse, TokenBudget
from crawlme.pioneer.ranker.llm import LLMRanker
from crawlme.schemas import URL, Candidate, CrawlGoal, RankHistorySummary


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


def _goal() -> CrawlGoal:
    return CrawlGoal(prompt="find recent rust compiler internals posts")


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


def _rankings_json(n: int, *, drop: list[str] | None = None, priority: float = 0.8) -> str:
    rankings = ", ".join(f'{{"id": "c{i}", "priority": {priority}, "rationale": "because {i}"}}' for i in range(n))
    drops = ", ".join(f'"{d}"' for d in (drop or []))
    return f'{{"rankings": [{rankings}], "candidates_to_drop": [{drops}]}}'


def _ranker(client: _StubClient, batch_size: int = 30, demote_dropped: bool = False) -> LLMRanker:
    return LLMRanker(client, batch_size=batch_size, demote_dropped=demote_dropped)


@pytest.mark.asyncio
async def test_ranks_batch_from_valid_json():
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
async def test_drop_list_marks_dropped():
    client = _StubClient([_resp(_rankings_json(1, drop=["c1"]))])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())
    by_id = {d.candidate_id: d for d in decisions}
    assert by_id["c1"].dropped is True
    assert by_id["c1"].priority == 0.0
    assert by_id["c1"].rationale == "llm_drop"


@pytest.mark.asyncio
async def test_drop_everything_when_batch_is_junk():
    content = '{"rankings": [], "candidates_to_drop": ["c0", "c1"]}'
    client = _StubClient([_resp(content)])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())
    assert all(d.dropped for d in decisions)


@pytest.mark.asyncio
async def test_rankings_win_when_id_in_both_lists():
    content = '{"rankings": [{"id": "c0", "priority": 0.7}], "candidates_to_drop": ["c0"]}'
    client = _StubClient([_resp(content)])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(1), RankHistorySummary())
    assert decisions[0].dropped is False
    assert decisions[0].priority == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_missing_ids_kept_at_neutral_priority():
    client = _StubClient([_resp(_rankings_json(1))])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())
    by_id = {d.candidate_id: d for d in decisions}
    assert by_id["c1"].dropped is False
    assert by_id["c1"].priority == pytest.approx(0.5)
    assert by_id["c1"].rationale == "no_opinion"


@pytest.mark.asyncio
async def test_unknown_ids_are_ignored():
    content = '{"rankings": [{"id": "ghost", "priority": 0.9}], "candidates_to_drop": ["phantom"]}'
    client = _StubClient([_resp(content)])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())
    assert len(decisions) == 2
    # Both candidates were unmentioned, so both fall to the neutral keep.
    assert all(d.rationale == "no_opinion" for d in decisions)


@pytest.mark.asyncio
async def test_priorities_clamped_to_unit_interval():
    content = '{"rankings": [{"id": "c0", "priority": 1.7}, {"id": "c1", "priority": -0.3}], "candidates_to_drop": []}'
    client = _StubClient([_resp(content)])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())
    by_id = {d.candidate_id: d for d in decisions}
    assert by_id["c0"].priority == 1.0
    assert by_id["c1"].priority == 0.0


@pytest.mark.asyncio
async def test_bool_priority_rejected():
    content = '{"rankings": [{"id": "c0", "priority": true}], "candidates_to_drop": []}'
    client = _StubClient([_resp(content)])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(1), RankHistorySummary())
    assert decisions[0].rationale == "no_opinion"


@pytest.mark.asyncio
async def test_prose_wrapped_json_is_tolerated():
    content = "Here are the scores:\n" + _rankings_json(2) + "\nHope that helps."
    client = _StubClient([_resp(content)])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())
    assert [d.candidate_id for d in decisions] == ["c0", "c1"]
    assert all(not d.dropped for d in decisions)


@pytest.mark.asyncio
async def test_trailing_comma_json_is_repaired():
    content = '{"rankings": [{"id": "c0", "priority": 0.6,},], "candidates_to_drop": [],}'
    client = _StubClient([_resp(content)])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(1), RankHistorySummary())
    assert decisions[0].priority == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_unparseable_retries_once_then_succeeds():
    client = _StubClient([_resp("not json at all"), _resp(_rankings_json(2))])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())
    assert len(client.calls) == 2
    assert "was not valid JSON" in client.calls[1]["prompt"]
    assert len(decisions) == 2


@pytest.mark.asyncio
async def test_unparseable_twice_raises():
    client = _StubClient([_resp("garbage"), _resp("still garbage")])
    with pytest.raises(LLMError, match="unparseable JSON"):
        await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())


@pytest.mark.asyncio
async def test_provider_error_propagates():
    client = _StubClient([LLMError("provider down")])
    with pytest.raises(LLMError):
        await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())


@pytest.mark.asyncio
async def test_chunks_large_batches():
    client = _StubClient([_resp(_rankings_json(2)), _resp(_rankings_json(2)), _resp(_rankings_json(1))])
    decisions = await _ranker(client, batch_size=2).rank_batch(_goal(), _candidates(5), RankHistorySummary())
    assert len(client.calls) == 3
    assert len(decisions) == 5
    # Each chunk is prompted separately; later chunks start fresh.
    assert "c2" in client.calls[1]["prompt"]
    assert "c4" in client.calls[2]["prompt"]


@pytest.mark.asyncio
async def test_empty_batch_makes_no_call():
    client = _StubClient([])
    decisions = await _ranker(client).rank_batch(_goal(), [], RankHistorySummary())
    assert decisions == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_prompt_carries_goal_candidates_and_json_mode():
    client = _StubClient([_resp(_rankings_json(2))])
    goal = _goal()
    await _ranker(client).rank_batch(goal, _candidates(2), RankHistorySummary())
    call = client.calls[0]
    assert call["json_mode"] is True
    assert call["max_tokens"] is None, "the client owns the ceiling, not each call site"
    assert "## Goal" in call["prompt"]
    assert goal.prompt in call["prompt"]
    assert "c0:" in call["prompt"] and "c1:" in call["prompt"]
    assert "anchor: link 0" in call["prompt"]
    assert "depth: 1" in call["prompt"]
    assert "compare" in call["system"]
    assert "candidates_to_drop" in call["system"]


@pytest.mark.asyncio
async def test_prompt_includes_relevant_history():
    history = RankHistorySummary(relevant_pages=[{"title": "Rust internals deep dive", "url": "https://x.com/a"}])
    client = _StubClient([_resp(_rankings_json(1))])
    await _ranker(client).rank_batch(_goal(), _candidates(1), history)
    assert "## Seen so far" in client.calls[0]["prompt"]
    assert "Rust internals deep dive" in client.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_prompt_includes_source_page_title():
    client = _StubClient([_resp(_rankings_json(1))])
    cands = _candidates(1)
    cands[0].source_url_key = "src1"
    await _ranker(client).rank_batch(
        _goal(),
        cands,
        RankHistorySummary(),
        page_contexts={"src1": {"title": "Compiler Blog"}},
    )
    assert "source page: Compiler Blog" in client.calls[0]["prompt"]


async def _prompt_with_source(src: dict) -> str:
    client = _StubClient([_resp(_rankings_json(1))])
    cands = _candidates(1)
    cands[0].source_url_key = "src1"
    await _ranker(client).rank_batch(_goal(), cands, RankHistorySummary(), page_contexts={"src1": src})
    return str(client.calls[0]["prompt"])


@pytest.mark.asyncio
async def test_prompt_carries_source_page_verdict():
    """2.9: the source page's judgment rides along with its title."""
    prompt = await _prompt_with_source(
        {
            "title": "Compiler Blog",
            "classification": "RELEVANT",
            "relevance": 0.9,
            "summary": "A deep dive into borrow checking.",
        }
    )
    assert "source page: Compiler Blog [RELEVANT 0.90] — A deep dive into borrow checking." in prompt


@pytest.mark.asyncio
async def test_prompt_omits_verdict_when_page_unanalyzed():
    """Regression: an unanalyzed source page yields the pre-2.9 line."""
    prompt = await _prompt_with_source({"title": "Compiler Blog", "link_count": 12})
    source_line = prompt.split("source page: ")[1].split("\n")[0]
    assert source_line == "Compiler Blog"


@pytest.mark.asyncio
async def test_prompt_keeps_verdict_without_summary():
    """A judgment with no summary still contributes the classification."""
    prompt = await _prompt_with_source({"title": "Nav", "classification": "NAVIGATION", "relevance": 0.0})
    source_line = prompt.split("source page: ")[1].split("\n")[0]
    assert source_line == "Nav [NAVIGATION 0.00]"


@pytest.mark.asyncio
async def test_prompt_truncates_source_summary():
    """Batches carry 30 candidates, so the summary must stay short."""
    prompt = await _prompt_with_source({"title": "T", "classification": "HUB", "relevance": 0.5, "summary": "x" * 200})
    assert "x" * 60 + "..." in prompt
    assert "x" * 61 not in prompt


def test_from_settings_auto_off_without_credentials():
    cfg = Settings(llm_api_key="", llm_base_url="")
    assert LLMRanker.from_settings(cfg) is None


def test_from_settings_wires_client_and_budget():
    cfg = Settings(llm_api_key="sk-test", llm_base_url="")
    budget = TokenBudget(limit=1000)
    ranker = LLMRanker.from_settings(cfg, budget=budget)
    assert ranker is not None
    assert ranker._client._budget is budget
    assert ranker._client._model  # provider default when llm_model is unset


async def test_a_truncated_rank_reply_is_retried_with_more_room():
    """Stricter wording cannot buy the room to finish the JSON.

    A run on a reasoning model spent the ceiling twice on the same
    prompt and fell back to embedding-only scores; the second attempt
    has to be bigger, not sterner.
    """
    cut_off = LLMResponse(content="{", input_tokens=100, output_tokens=4096, model="stub", truncated=True)
    client = _StubClient([cut_off, _resp(_rankings_json(2))])
    await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())

    retry = client.calls[1]
    assert retry["max_tokens"] == 8192, "twice what the cut-off reply managed"
    assert retry["prompt"] == client.calls[0]["prompt"], "the wording was never the problem"


async def test_unparseable_but_complete_still_gets_the_stricter_wording():
    """A short reply that is simply malformed is a wording problem."""
    malformed = LLMResponse(content="not json", input_tokens=100, output_tokens=20, model="stub", truncated=False)
    client = _StubClient([malformed, _resp(_rankings_json(2))])
    await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())

    retry = client.calls[1]
    assert retry["max_tokens"] is None
    assert retry["prompt"] != client.calls[0]["prompt"]


#: recall mode ------------------------------------------------------------


async def test_recall_mode_ranks_a_rejection_last_instead_of_removing_it():
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


async def test_a_rejection_still_ranks_below_a_candidate_nobody_judged():
    """Silence is weaker evidence than an argument against."""
    body = '{"rankings": [], "candidates_to_drop": ["c1"]}'
    client = _StubClient([_resp(body)])
    decisions = await _ranker(client, demote_dropped=True).rank_batch(_goal(), _candidates(2), RankHistorySummary())

    by_id = {d.candidate_id: d for d in decisions}
    assert by_id["c0"].rationale == "no_opinion"
    assert by_id["c1"].priority < by_id["c0"].priority


async def test_without_recall_a_rejection_is_still_a_removal():
    body = '{"rankings": [], "candidates_to_drop": ["c1"]}'
    client = _StubClient([_resp(body)])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(2), RankHistorySummary())
    assert {d.candidate_id for d in decisions if d.dropped} == {"c1"}


async def test_a_rejection_carries_the_reason_it_was_rejected():
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


async def test_a_bare_id_is_still_a_rejection():
    """What a model returns when it ignores the shape it was asked for."""
    client = _StubClient([_resp('{"rankings": [], "candidates_to_drop": ["c0"]}')])
    decisions = await _ranker(client).rank_batch(_goal(), _candidates(1), RankHistorySummary())
    assert decisions[0].dropped is True
    assert decisions[0].rationale == "llm_drop"


async def test_a_demoted_rejection_keeps_both_the_tag_and_the_reason():
    body = '{"rankings": [], "candidates_to_drop": [{"id": "c0", "rationale": "weaker than the rest"}]}'
    client = _StubClient([_resp(body)])
    decisions = await _ranker(client, demote_dropped=True).rank_batch(_goal(), _candidates(1), RankHistorySummary())
    assert decisions[0].rationale == "llm_drop_demoted: weaker than the rest"
    assert decisions[0].dropped is False
