"""Tests for the LLM client wrapper, with a stub provider.

litellm is never imported here: the provider is injected by patching
the module's cached _litellm reference, so the suite runs without the
optional dependency installed.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
from types import SimpleNamespace

import httpx
import pytest

import crawlme.llm.client as llm_mod
from crawlme.analyzer import PageAnalyzer
from crawlme.config import Settings
from crawlme.llm import LLMClient, LLMError, TokenBudget, TokenBudgetError


class _StubLitellm:
    """Fake litellm module with a scripted acompletion.

    The script is a list of responses (SimpleNamespace) or exceptions,
    consumed one per call.  Tracks active calls to test the semaphore.
    """

    RateLimitError = type("RateLimitError", (Exception,), {})
    Timeout = type("Timeout", (Exception,), {})
    APIConnectionError = type("APIConnectionError", (Exception,), {})
    ServiceUnavailableError = type("ServiceUnavailableError", (Exception,), {})
    InternalServerError = type("InternalServerError", (Exception,), {})

    def __init__(self, script: list, hold: float = 0.0) -> None:
        self._script = list(script)
        self._hold = hold
        self.kwargs: list[dict] = []
        self.active = 0
        self.max_active = 0

    async def acompletion(self, **kwargs):
        self.kwargs.append(dict(kwargs))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self._hold:
                await asyncio.sleep(self._hold)
            item = self._script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        finally:
            self.active -= 1


def _resp(content: str, in_tok: int = 10, out_tok: int = 5, model: str = "stub-model"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=in_tok, completion_tokens=out_tok),
        model=model,
    )


@pytest.fixture
def provider(monkeypatch):
    stub = _StubLitellm([_resp("hello")])
    monkeypatch.setattr(llm_mod, "_litellm", stub)
    return stub


@pytest.fixture
def no_sleep(monkeypatch):
    calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(llm_mod, "_sleep", fake_sleep)
    return calls


async def test_chat_returns(provider, no_sleep):
    client = LLMClient("openai/gpt-4o-mini")
    r = await client.chat("hello?", system="be brief")
    assert r.content == "hello"
    assert r.input_tokens == 10
    assert r.output_tokens == 5
    # The configured model is the recorded identity; the reported name
    # is only a fallback when nothing is configured.
    assert r.model == "openai/gpt-4o-mini"
    sent = provider.kwargs[0]
    assert sent["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello?"},
    ]
    assert sent["max_tokens"] == 8192, "the configured ceiling, not a per-call-site constant"
    assert sent["model"] == "openai/gpt-4o-mini"
    assert "response_format" not in sent
    assert no_sleep == []


async def test_model_wins(provider, no_sleep):
    # Providers may answer under an alias for the configured model
    # (deepseek/deepseek-chat -> deepseek-v4-flash); the configured
    # string is the stable identity replay's check matches on.
    client = LLMClient("deepseek/deepseek-chat")
    r = await client.chat("hi")
    assert r.model == "deepseek/deepseek-chat"


async def test_model_reported(provider, no_sleep):
    client = LLMClient("")
    r = await client.chat("hi")
    assert r.model == "stub-model"


async def test_json_mode(provider):
    client = LLMClient("openai/gpt-4o-mini")
    await client.chat("return json", json_mode=True)
    assert provider.kwargs[0]["response_format"] == {"type": "json_object"}


async def test_creds_optional(monkeypatch):
    stub = _StubLitellm([_resp("a"), _resp("b")])
    monkeypatch.setattr(llm_mod, "_litellm", stub)

    bare = LLMClient("openai/gpt-4o-mini")
    await bare.chat("hi")
    assert "api_key" not in stub.kwargs[0]
    assert "api_base" not in stub.kwargs[0]

    wired = LLMClient("openai/gpt-4o-mini", api_key="sk-1", base_url="http://localhost:11434/v1")
    await wired.chat("hi")
    assert stub.kwargs[1]["api_key"] == "sk-1"
    assert stub.kwargs[1]["api_base"] == "http://localhost:11434/v1"


async def test_retry_succeeds(monkeypatch, no_sleep):
    stub = _StubLitellm([_StubLitellm.RateLimitError(), _resp("ok")])
    monkeypatch.setattr(llm_mod, "_litellm", stub)
    r = await LLMClient("openai/gpt-4o-mini").chat("hi")
    assert r.content == "ok"
    assert len(stub.kwargs) == 2
    assert no_sleep == [1.0]


async def test_retry_exhausted(monkeypatch, no_sleep):
    stub = _StubLitellm([_StubLitellm.RateLimitError(), _StubLitellm.Timeout(), _StubLitellm.ServiceUnavailableError()])
    monkeypatch.setattr(llm_mod, "_litellm", stub)
    with pytest.raises(LLMError, match="3 attempts"):
        await LLMClient("openai/gpt-4o-mini").chat("hi")
    assert len(stub.kwargs) == 3
    assert no_sleep == [1.0, 2.0]


async def test_connect_retried(monkeypatch, no_sleep):
    stub = _StubLitellm([httpx.ConnectError("provider down"), _resp("ok")])
    monkeypatch.setattr(llm_mod, "_litellm", stub)
    r = await LLMClient("openai/gpt-4o-mini").chat("hi")
    assert r.content == "ok"
    assert no_sleep == [1.0]


async def test_permanent_raises(monkeypatch, no_sleep):
    stub = _StubLitellm([ValueError("bad request body")])
    monkeypatch.setattr(llm_mod, "_litellm", stub)
    with pytest.raises(LLMError, match="rejected"):
        await LLMClient("openai/gpt-4o-mini").chat("hi")
    assert len(stub.kwargs) == 1
    assert no_sleep == []


async def test_no_creds_raises(monkeypatch, no_sleep):
    # litellm maps missing credentials to InternalServerError, which is
    # normally transient.  The message check must win.
    stub = _StubLitellm([_StubLitellm.InternalServerError("Missing credentials. Please pass an `api_key`.")])
    monkeypatch.setattr(llm_mod, "_litellm", stub)
    with pytest.raises(LLMError, match="rejected"):
        await LLMClient("openai/gpt-4o-mini").chat("hi")
    assert len(stub.kwargs) == 1
    assert no_sleep == []


async def test_concurrency_cap(monkeypatch):
    stub = _StubLitellm([_resp("r") for _ in range(6)], hold=0.01)
    monkeypatch.setattr(llm_mod, "_litellm", stub)
    client = LLMClient("openai/gpt-4o-mini", concurrency=2)
    results = await asyncio.gather(*[client.chat("hi") for _ in range(6)])
    assert all(r.content == "r" for r in results)
    assert stub.max_active == 2


async def test_no_litellm(monkeypatch):
    monkeypatch.setattr(llm_mod, "_litellm", None)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "litellm":
            raise ImportError("no litellm")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(LLMError, match="litellm"):
        await LLMClient("openai/gpt-4o-mini").chat("hi")


def test_wires_knobs():
    settings = Settings(
        llm_model="anthropic/claude-haiku-4-5",
        llm_api_key="sk-test",
        llm_base_url="http://localhost:11434/v1",
        llm_concurrency=5,
    )
    client = LLMClient.from_settings(settings)
    assert client._model == "anthropic/claude-haiku-4-5"
    assert client._api_key == "sk-test"
    assert client._base_url == "http://localhost:11434/v1"
    assert client._sem._value == 5


def test_budget_records():
    totals: list[int] = []
    budget = TokenBudget(limit=100, sink=totals.append)
    budget.record(10, 5)
    assert budget.used == 15
    assert budget.input_tokens == 10
    assert budget.output_tokens == 5
    assert totals == [15]
    budget.record(0, 0)
    assert budget.used == 15
    assert budget.calls == 2


def test_budget_limit():
    budget = TokenBudget(limit=10)
    budget.record(10, 0)
    with pytest.raises(TokenBudgetError):
        budget.check()
    # limit 0 means uncapped.
    uncapped = TokenBudget(limit=0)
    uncapped.record(9999, 0)
    uncapped.check()


async def test_budget_blocks(monkeypatch, no_sleep):
    stub = _StubLitellm([_resp("hi")])
    monkeypatch.setattr(llm_mod, "_litellm", stub)
    budget = TokenBudget(limit=1)
    budget.record(1, 0)
    with pytest.raises(TokenBudgetError):
        await LLMClient("openai/gpt-4o-mini", budget=budget).chat("hi")
    assert stub.kwargs == []
    assert no_sleep == []


async def test_chat_tallies(monkeypatch):
    stub = _StubLitellm([_resp("hello", in_tok=12, out_tok=7)])
    monkeypatch.setattr(llm_mod, "_litellm", stub)
    budget = TokenBudget(limit=1000)
    await LLMClient("openai/gpt-4o-mini", budget=budget).chat("hi")
    assert budget.used == 19
    assert budget.calls == 1


def test_is_configured():
    assert not LLMClient("openai/gpt-4o-mini").configured
    assert LLMClient("openai/gpt-4o-mini", api_key="sk-1").configured
    assert LLMClient("openai/gpt-4o-mini", base_url="http://localhost:11434/v1").configured


@pytest.mark.parametrize(
    ("api_key", "base_url", "built"),
    [
        # Explicit empties beat whatever the developer's .env holds.
        ("", "", False),
        # A key alone is enough; so is a local endpoint that wants none.
        ("sk-1", "", True),
        ("", "http://localhost:11434/v1", True),
    ],
)
def test_settings_gate(api_key, base_url, built):
    settings = Settings(llm_model="", llm_api_key=api_key, llm_base_url=base_url)
    client = LLMClient.from_settings_if_configured(settings)
    assert (client is not None) is built
    if client is not None:
        assert client._api_key == api_key
        assert client._base_url == base_url
        assert client._model == "openai/gpt-4o-mini"  # falls back to the provider default


def test_default_model():
    assert LLMClient.from_settings(Settings(llm_model=""))._model == "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_ceiling_flagged(monkeypatch, caplog):
    """Otherwise a budget problem arrives disguised as a parser one.

    A reasoning model spends the ceiling on thinking, and what comes back
    is empty or cut off mid-JSON. Callers that only see unparseable text
    go looking at the parser, which is the wrong place.
    """
    monkeypatch.setattr(llm_mod, "_litellm", _StubLitellm([_resp("{", out_tok=64)]))
    client = LLMClient("openai/gpt-4o-mini", api_key="k", max_output_tokens=64)
    with caplog.at_level(logging.WARNING):
        resp = await client.chat("hi")
    assert resp.truncated is True
    assert "output_ceiling" in caplog.text


@pytest.mark.asyncio
async def test_under_ceiling(monkeypatch):
    monkeypatch.setattr(llm_mod, "_litellm", _StubLitellm([_resp("ok", out_tok=5)]))
    client = LLMClient("openai/gpt-4o-mini", api_key="k", max_output_tokens=64)
    assert (await client.chat("hi")).truncated is False


# cached input ----------------------------------------------------------


def test_cached_tallied():
    """Cached input counts the same and costs about a tenth, so a total
    that does not separate it is not a bill."""
    budget = TokenBudget(limit=0)
    budget.record(1000, 100, cached_tokens=800)
    budget.record(1000, 100)

    assert budget.input_tokens == 2000
    assert budget.cached_input_tokens == 800
    assert budget.used == 2200


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (SimpleNamespace(prompt_cache_hit_tokens=512), 512),
        (SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens=384)), 384),
        (SimpleNamespace(prompt_tokens_details={"cached_tokens": 256}), 256),
        ({"prompt_cache_hit_tokens": 128}, 128),
        (SimpleNamespace(prompt_tokens=900), 0),
    ],
)
def test_cached_shapes(usage, expected):
    """Providers disagree on where they put it.  One that reports
    nothing gives 0, which reads as "not measured" rather than "nothing
    was cached" -- a distinction worth a third of the bill."""
    assert llm_mod._cached_input(usage) == expected


@pytest.mark.parametrize(
    ("resp", "usage", "expected"),
    [
        (SimpleNamespace(), SimpleNamespace(completion_tokens_details=SimpleNamespace(reasoning_tokens=700)), 700),
        (SimpleNamespace(), SimpleNamespace(completion_tokens_details={"reasoning_tokens": 512}), 512),
        (
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(reasoning_content="x" * 400))]),
            SimpleNamespace(completion_tokens=900),
            100,
        ),
        (SimpleNamespace(), SimpleNamespace(completion_tokens=900), 0),
    ],
)
def test_reasoning_read(resp, usage, expected):
    """Thinking is billed as output and then dropped, so it is counted
    from the usage field when there is one and from what arrived when
    there is not."""
    assert llm_mod._reasoning_output(resp, usage) == expected


def test_thinking_apart():
    budget = TokenBudget(limit=0)
    budget.record(100, 1000, cached_tokens=40, reasoning_tokens=800)

    assert budget.output_tokens == 1000
    assert budget.reasoning_tokens == 800
    assert budget.cached_input_tokens == 40


@pytest.mark.asyncio
async def test_effort_optional(monkeypatch):
    """Empty means send nothing, which is the provider's default and
    what every run before the setting existed paid for."""
    sent: list[dict] = []

    async def _fake(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            model="m",
        )

    monkeypatch.setattr(llm_mod, "_litellm_module", lambda: SimpleNamespace(acompletion=_fake))

    await LLMClient("m", api_key="k").chat("hi")
    assert "reasoning_effort" not in sent[-1]

    await LLMClient("m", api_key="k", reasoning_effort="minimal").chat("hi")
    assert sent[-1]["reasoning_effort"] == "minimal"


def test_effort_stage(monkeypatch):
    """One stage orders candidates for fetching and another decides what
    the run returns, so they do not have to buy the same thinking."""
    from crawlme.pioneer.ranker import LLMRanker

    cfg = Settings(llm_api_key="k", llm_analyze_reasoning_effort="high", llm_rank_reasoning_effort="none")
    ranker = LLMRanker.from_settings(cfg)
    assert ranker is not None
    assert ranker._client._reasoning_effort == "none"

    assert PageAnalyzer.from_settings(cfg)._client._reasoning_effort == "high"  # type: ignore[union-attr]


def test_effort_absent():
    """Empty means the provider's own default, not some value of ours."""
    from crawlme.pioneer.ranker import LLMRanker

    # Spelled out rather than left to the default: Settings reads the
    # developer's own .env, so a test that says nothing about a field
    # asserts whatever that machine happens to hold.
    ranker = LLMRanker.from_settings(Settings(llm_api_key="k", llm_rank_reasoning_effort=""))
    assert ranker is not None
    assert ranker._client._reasoning_effort == ""
