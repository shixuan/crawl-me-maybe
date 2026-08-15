"""Tests for the PageAnalyzer with a stub LLM client.

The analyzer depends only on the chat(prompt, system=..., ...)
interface, so the client is faked with a scripted responder.
"""

from __future__ import annotations

import asyncio

import pytest

from crawlme.config import Settings
from crawlme.feedback.analyzer import PageAnalyzer
from crawlme.llm import LLMError, LLMResponse, TokenBudget, TokenBudgetError
from crawlme.schemas import URL, CrawlGoal, Page


class _StubClient:
    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    async def chat(self, prompt: str, *, system: str = "", max_tokens: int = 512, json_mode: bool = False):
        self.calls.append({"prompt": prompt, "system": system, "json_mode": json_mode})
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, input_tokens=300, output_tokens=60, model="stub")


def _goal() -> CrawlGoal:
    goal = CrawlGoal(prompt="find rust compiler internals posts")
    goal.goal_statement = "Find Rust compiler internals posts"
    return goal


def _page(text: str = "The Rust compiler borrow checker explained in detail.") -> Page:
    return Page(
        page_id="p1",
        url_key="k1",
        url=URL(
            raw="https://example.com/rust",
            canonical="https://example.com/rust",
            url_key="k1",
            reg_domain="example.com",
        ),
        title="Borrow Checker Deep Dive",
        plain_text=text,
    )


def _valid_json() -> str:
    return (
        '{"classification": "RELEVANT", "relevance_score": 0.9, "hub_score": 0.3, '
        '"summary": "Explains the borrow checker.", "tags": ["rust", "compiler"], '
        '"topics": ["borrow checking"], "entities": ["rustc"], '
        '"endorsed_links": ["https://example.com/next"]}'
    )


def _analyzer(client: _StubClient, *, retry_delay: float = 0.0) -> PageAnalyzer:
    return PageAnalyzer(client, retry_delay=retry_delay)


# -- happy path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_builds_full_result():
    client = _StubClient([_resp(_valid_json())])
    result = await _analyzer(client).analyze(_page(), _goal())
    assert result is not None
    assert result.classification == "RELEVANT"
    assert result.relevance_score == pytest.approx(0.9)
    assert result.summary == "Explains the borrow checker."
    assert result.tags == ["rust", "compiler"]
    assert result.model == "stub"
    assert result.tokens_used == 360
    assert result.prompt_version
    assert result.page_id == "p1"
    assert result.url_key == "k1"
    assert result.goal_id
    # Feedback carries the scheduler-facing signals.
    fb = result.feedback
    assert fb.classification == "RELEVANT"
    assert fb.hub_score == pytest.approx(0.3)
    assert fb.endorsed_links == ["https://example.com/next"]
    assert fb.topics == ["borrow checking"]
    assert fb.entities == ["rustc"]
    assert fb.domain == "example.com"
    # Page identity rides along so the feedback signals can build the
    # ranker's "seen so far" history without the Page itself.
    assert fb.url == "https://example.com/rust"
    assert fb.title == "Borrow Checker Deep Dive"


@pytest.mark.asyncio
async def test_prompt_carries_goal_page_and_json_mode():
    client = _StubClient([_resp(_valid_json())])
    goal = _goal()
    await _analyzer(client).analyze(_page(), goal)
    call = client.calls[0]
    assert call["json_mode"] is True
    assert "Find Rust compiler internals posts" in call["prompt"]  # statement preferred
    assert "https://example.com/rust" in call["prompt"]
    assert "Borrow Checker Deep Dive" in call["prompt"]
    assert "borrow checker" in call["prompt"]
    assert "RELEVANT" in call["system"]
    assert "endorsed_links" in call["system"]


@pytest.mark.asyncio
async def test_page_text_truncated_in_prompt():
    long_text = "word " * 4000  # 20k chars
    client = _StubClient([_resp(_valid_json())])
    await _analyzer(client).analyze(_page(long_text), _goal())
    prompt = client.calls[0]["prompt"]
    assert len(prompt) < 7000  # 6000-char cap plus headers


@pytest.mark.asyncio
async def test_custom_page_char_cap_is_honored():
    """The settings knob dials the page-text cap (the benchmark's C arm)."""
    long_text = "word " * 4000
    client = _StubClient([_resp(_valid_json())])
    analyzer = PageAnalyzer(client, retry_delay=0.0, max_page_chars=200)
    await analyzer.analyze(_page(long_text), _goal())
    prompt = client.calls[0]["prompt"]
    assert len(prompt) < 400  # 200-char cap plus headers


@pytest.mark.asyncio
async def test_empty_page_skipped_without_call():
    client = _StubClient([])
    result = await _analyzer(client).analyze(_page(""), _goal())
    assert result is None
    assert client.calls == []


# -- parsing tolerance --------------------------------------------------


@pytest.mark.asyncio
async def test_prose_wrapped_json_is_tolerated():
    content = "Sure:\n" + _valid_json() + "\nDone."
    client = _StubClient([_resp(content)])
    result = await _analyzer(client).analyze(_page(), _goal())
    assert result is not None
    assert result.classification == "RELEVANT"


@pytest.mark.asyncio
async def test_unknown_classification_degrades_to_unknown():
    content = '{"classification": "totally-relevant", "relevance_score": 0.5}'
    client = _StubClient([_resp(content)])
    result = await _analyzer(client).analyze(_page(), _goal())
    assert result is not None
    assert result.classification == "UNKNOWN"
    assert result.feedback.classification == "UNKNOWN"


@pytest.mark.asyncio
async def test_scores_clamped_to_unit_interval():
    content = '{"classification": "HUB", "relevance_score": 1.7, "hub_score": -0.3}'
    client = _StubClient([_resp(content)])
    result = await _analyzer(client).analyze(_page(), _goal())
    assert result is not None
    assert result.relevance_score == 1.0
    assert result.feedback.hub_score == 0.0


@pytest.mark.asyncio
async def test_bool_score_rejected():
    content = '{"classification": "HUB", "relevance_score": true, "hub_score": false}'
    client = _StubClient([_resp(content)])
    result = await _analyzer(client).analyze(_page(), _goal())
    assert result is not None
    assert result.relevance_score == 0.0
    assert result.feedback.hub_score == 0.0


@pytest.mark.asyncio
async def test_missing_fields_fall_back_to_defaults():
    content = "{}"
    client = _StubClient([_resp(content)])
    result = await _analyzer(client).analyze(_page(), _goal())
    assert result is not None
    assert result.classification == "UNKNOWN"
    assert result.summary == ""
    assert result.tags == []
    assert result.feedback.endorsed_links == []


@pytest.mark.asyncio
async def test_lists_deduplicated_and_capped():
    topics = ", ".join('"t"' for _ in range(15))
    content = (
        '{"classification": "HUB", "tags": ["a", "a", "b", "c", "d", "e", "f", "g", "h", "i"], '
        f'"topics": [{topics}], '
        '"entities": ["e1", "e2", "e3", "e4", "e5", "e6", "e7", "e8", "e9", "e10", "e11"], '
        '"endorsed_links": ["u1", "u1", "u2", "u3", "u4", "u5", "u6", 42]}'
    )
    client = _StubClient([_resp(content)])
    result = await _analyzer(client).analyze(_page(), _goal())
    assert result is not None
    assert result.tags == ["a", "b", "c", "d", "e", "f", "g", "h"]  # 8 cap, deduped
    assert result.feedback.topics == ["t"]  # 10 cap, deduped
    assert len(result.feedback.entities) == 10
    assert result.feedback.endorsed_links == ["u1", "u2", "u3", "u4", "u5"]  # 5 cap, non-str dropped


# -- failure and retry policy --------------------------------------------


@pytest.mark.asyncio
async def test_failure_parks_page_and_background_retry_succeeds():
    client = _StubClient([LLMError("provider down"), _resp(_valid_json())])
    analyzer = _analyzer(client)
    published: list = []
    analyzer.bind_sink(published.append)

    assert await analyzer.analyze(_page(), _goal()) is None
    for _ in range(100):
        if published:
            break
        await asyncio.sleep(0.01)

    assert len(client.calls) == 2
    assert len(published) == 1
    assert published[0].classification == "RELEVANT"


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts():
    client = _StubClient([LLMError("down"), LLMError("down"), LLMError("down")])
    analyzer = _analyzer(client)
    published: list = []
    analyzer.bind_sink(published.append)

    assert await analyzer.analyze(_page(), _goal()) is None
    for _ in range(100):
        if len(client.calls) >= 3:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)  # give the giveup path a beat after the last call

    assert len(client.calls) == 3
    assert published == []


@pytest.mark.asyncio
async def test_token_budget_exhausted_is_not_requeued():
    client = _StubClient([TokenBudgetError("budget exhausted")])
    analyzer = _analyzer(client)

    assert await analyzer.analyze(_page(), _goal()) is None
    await asyncio.sleep(0.05)

    assert len(client.calls) == 1  # no background retry behind a dead budget


@pytest.mark.asyncio
async def test_unparseable_json_is_treated_as_failure():
    client = _StubClient([_resp("not json"), _resp(_valid_json())])
    analyzer = _analyzer(client)
    published: list = []
    analyzer.bind_sink(published.append)

    assert await analyzer.analyze(_page(), _goal()) is None
    for _ in range(100):
        if published:
            break
        await asyncio.sleep(0.01)

    assert published  # retry succeeded


@pytest.mark.asyncio
async def test_aclose_cancels_parked_retries():
    client = _StubClient([LLMError("down"), _resp(_valid_json())])
    analyzer = _analyzer(client, retry_delay=60.0)

    assert await analyzer.analyze(_page(), _goal()) is None
    await analyzer.aclose()
    await asyncio.sleep(0.05)

    assert len(client.calls) == 1  # parked page dropped, never retried


@pytest.mark.asyncio
async def test_sink_receives_first_try_results():
    client = _StubClient([_resp(_valid_json())])
    analyzer = _analyzer(client)
    published: list = []
    analyzer.bind_sink(published.append)

    result = await analyzer.analyze(_page(), _goal())
    assert result is not None
    assert published == [result]


# -- construction -------------------------------------------------------


def test_from_settings_auto_off_without_credentials():
    cfg = Settings(llm_api_key="", llm_base_url="")
    assert PageAnalyzer.from_settings(cfg) is None


def test_from_settings_wires_client_and_budget():
    cfg = Settings(llm_api_key="sk-test", llm_base_url="")
    budget = TokenBudget(limit=1000)
    analyzer = PageAnalyzer.from_settings(cfg, budget=budget)
    assert analyzer is not None
    assert analyzer._client._budget is budget
    assert analyzer._max_page_chars == 6000  # default knob


def test_from_settings_wires_max_page_chars():
    cfg = Settings(llm_api_key="sk-test", llm_base_url="", analyzer_max_chars=3000)
    analyzer = PageAnalyzer.from_settings(cfg)
    assert analyzer is not None
    assert analyzer._max_page_chars == 3000
