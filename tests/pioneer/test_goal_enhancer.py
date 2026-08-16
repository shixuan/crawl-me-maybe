"""Tests for the Goal Enhancer, with a stub LLM client.

The enhancer depends only on the chat(prompt, system=..., ...)
interface, so the client is faked with a scripted responder.
"""

from __future__ import annotations

import datetime

from crawlme.llm import LLMError, LLMResponse, TokenBudgetError
from crawlme.pioneer.goal_enhancer import GoalEnhancer
from crawlme.schemas import CrawlGoal


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
    return LLMResponse(content=content, input_tokens=10, output_tokens=5, model="stub")


def _goal(prompt: str = "find machine learning papers") -> CrawlGoal:
    return CrawlGoal(prompt=prompt)


def _valid_json() -> str:
    return (
        '{"goal_statement": "Find machine learning research papers", '
        '"keywords": ["machine learning", "papers"], "since": null}'
    )


async def test_no_client_is_inert():
    enhanced = await GoalEnhancer(None).enhance(_goal())
    assert enhanced is None


async def test_valid_json_fills_all_fields():
    enhancer = GoalEnhancer(_StubClient([_resp(_valid_json())]))
    enhanced = await enhancer.enhance(_goal("找机器学习论文"))
    assert enhanced is not None
    assert enhanced.statement == "Find machine learning research papers"
    assert enhanced.keywords == ["machine learning", "papers"]
    assert enhanced.since is None


async def test_chat_called_with_system_and_json_mode():
    client = _StubClient([_resp(_valid_json())])
    await GoalEnhancer(client).enhance(_goal())
    call = client.calls[0]
    assert call["prompt"] == "find machine learning papers"
    assert call["json_mode"] is True
    assert "JSON" in call["system"]
    # The model cannot know today's date: the prompt must carry it so
    # time-window goals resolve since correctly.
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    assert f"Today is {today}" in call["system"]


async def test_prose_wrapped_around_json_is_tolerated():
    content = "Sure, here you go:\n" + _valid_json() + "\nHope that helps."
    enhanced = await GoalEnhancer(_StubClient([_resp(content)])).enhance(_goal())
    assert enhanced is not None
    assert enhanced.keywords == ["machine learning", "papers"]


async def test_llm_error_returns_none():
    enhancer = GoalEnhancer(_StubClient([LLMError("provider down")]))
    assert await enhancer.enhance(_goal()) is None


async def test_token_budget_exceeded_returns_none():
    enhancer = GoalEnhancer(_StubClient([TokenBudgetError("token budget exhausted")]))
    assert await enhancer.enhance(_goal()) is None


async def test_garbage_response_returns_none():
    assert await GoalEnhancer(_StubClient([_resp("not json at all")])).enhance(_goal()) is None
    assert await GoalEnhancer(_StubClient([_resp("{broken")])).enhance(_goal()) is None


async def test_empty_statement_returns_none():
    content = '{"goal_statement": "", "keywords": ["machine learning"]}'
    assert await GoalEnhancer(_StubClient([_resp(content)])).enhance(_goal()) is None


async def test_keywords_sanitized():
    content = (
        '{"goal_statement": "s", "keywords": ["ml", "ml", "", "papers", 42, "k3", "k4", "k5", "k6", "k7", '
        '"k8", "k9", "k10", "k11", "k12", "k13"]}'
    )
    enhanced = await GoalEnhancer(_StubClient([_resp(content)])).enhance(_goal())
    assert enhanced is not None
    assert enhanced.keywords == ["ml", "papers", "k3", "k4", "k5", "k6", "k7", "k8", "k9", "k10", "k11", "k12"]


async def test_missing_keywords_fall_back_to_bare_tokenization():
    content = '{"goal_statement": "Find Rust jobs", "since": null}'
    enhanced = await GoalEnhancer(_StubClient([_resp(content)])).enhance(_goal("find rust jobs"))
    assert enhanced is not None
    assert enhanced.keywords == ["find", "rust", "jobs"]


async def test_since_parsed_as_aware_utc():
    content = '{"goal_statement": "s", "keywords": ["k"], "since": "2026-07-01"}'
    enhanced = await GoalEnhancer(_StubClient([_resp(content)])).enhance(_goal())
    assert enhanced is not None
    assert enhanced.since == datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)


async def test_since_rejects_future_and_ancient_dates():
    future = '{"goal_statement": "s", "keywords": ["k"], "since": "2999-01-01"}'
    ancient = '{"goal_statement": "s", "keywords": ["k"], "since": "1990-01-01"}'
    garbage = '{"goal_statement": "s", "keywords": ["k"], "since": "not a date"}'
    for content in (future, ancient, garbage):
        enhanced = await GoalEnhancer(_StubClient([_resp(content)])).enhance(_goal())
        assert enhanced is not None
        assert enhanced.since is None
