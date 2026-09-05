"""Translating this project's effort into what a model accepts."""

from __future__ import annotations

import pytest

from crawlme.llm.reasoning import effort_for


@pytest.fixture
def catalogue(monkeypatch):
    """Stand in for litellm's per-model answers."""
    supported: dict[str, list[str]] = {}
    info: dict[str, dict] = {}

    class _Fake:
        @staticmethod
        def get_supported_openai_params(model):
            return supported.get(model, ["reasoning_effort"])

        @staticmethod
        def get_model_info(model):
            return info.get(model, {})

    import sys

    monkeypatch.setitem(sys.modules, "litellm", _Fake)
    return supported, info


def test_nothing_wanted_sends_nothing(catalogue):
    assert effort_for("any/model", "") == ""


def test_a_model_that_cannot_think_gets_no_parameter(catalogue):
    """It rejects the parameter itself, so naming a level would fail the
    call rather than lower it."""
    supported, _ = catalogue
    supported["openai/gpt-4o-mini"] = ["temperature"]
    assert effort_for("openai/gpt-4o-mini", "off") == ""
    assert effort_for("openai/gpt-4o-mini", "medium") == ""


def test_off_becomes_the_floor_the_model_accepts(catalogue):
    """DeepSeek turns thinking off with "none"; GPT-5 rejects that value
    and calls its own floor "minimal"."""
    _, info = catalogue
    info["openai/gpt-5"] = {"supports_none_reasoning_effort": False, "supports_minimal_reasoning_effort": True}
    assert effort_for("openai/gpt-5", "off") == "minimal"
    assert effort_for("deepseek/deepseek-chat", "off") == "none"


def test_a_level_passes_through(catalogue):
    assert effort_for("openai/gpt-5", "medium") == "medium"
    assert effort_for("deepseek/deepseek-chat", "high") == "high"


def test_a_model_refusing_every_floor_keeps_thinking(catalogue):
    """Failing the call is worse than thinking."""
    _, info = catalogue
    info["openai/o3"] = {"supports_none_reasoning_effort": False, "supports_minimal_reasoning_effort": False}
    assert effort_for("openai/o3", "off") == ""


def test_an_uncatalogued_model_is_given_the_benefit(catalogue):
    """litellm records the answer for a handful of models and nothing
    for the rest, so silence must not read as refusal."""
    assert effort_for("someone/new-model", "off") == "none"


def test_a_lookup_that_raises_does_not_fail_the_call(monkeypatch):
    class _Broken:
        @staticmethod
        def get_supported_openai_params(model):
            raise RuntimeError("no catalogue")

        @staticmethod
        def get_model_info(model):
            raise RuntimeError("no catalogue")

    import sys

    monkeypatch.setitem(sys.modules, "litellm", _Broken)
    assert effort_for("any/model", "off") == "none"


def test_a_skipped_setting_is_announced(catalogue, caplog):
    """A model whose catalogue entry is missing looks exactly like one
    that cannot think, so a new model could silently keep thinking."""
    supported, _ = catalogue
    supported["openai/gpt-7"] = ["temperature"]
    with caplog.at_level("INFO"):
        assert effort_for("openai/gpt-7", "off") == ""
    assert "llm.reasoning_skipped" in caplog.text
