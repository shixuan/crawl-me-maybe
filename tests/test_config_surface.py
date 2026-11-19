"""Environment settings and compatibility precedence."""

from __future__ import annotations

import pytest

from crawlme.config import Settings


@pytest.mark.parametrize("prefix", ["EXPAND", "ENHANCE"])
@pytest.mark.parametrize("source", ["env", "dotenv"])
def test_seed_expansion_settings(prefix, source, tmp_path, monkeypatch):
    for name in ("EXPAND", "ENHANCE"):
        for suffix in ("", "_COUNT"):
            monkeypatch.delenv(f"{name}_SEEDS{suffix}", raising=False)
    monkeypatch.delenv("EXPAND_SEARCH_COUNT", raising=False)
    values = {f"{prefix}_SEEDS": "true", "EXPAND_SEEDS_COUNT": "7", "EXPAND_SEARCH_COUNT": "5"}
    env_file = tmp_path / ".env"
    if source == "env":
        for key, value in values.items():
            monkeypatch.setenv(key, value)
    else:
        env_file.write_text("\n".join(f"{key}={value}" for key, value in values.items()))
    cfg = Settings(_env_file=env_file, llm_api_key="", llm_base_url="", llm_model="")
    assert cfg.expand_seeds is True
    assert cfg.expand_seeds_count == 7
    assert cfg.expand_search_count == 5


def test_new_seed_setting_wins(monkeypatch):
    monkeypatch.setenv("EXPAND_SEEDS", "true")
    monkeypatch.setenv("ENHANCE_SEEDS", "false")
    cfg = Settings(_env_file=None, llm_api_key="", llm_base_url="", llm_model="")
    assert cfg.expand_seeds is True


@pytest.mark.parametrize(
    "value,expected", [("", []), ("one.json", ["one.json"]), ('["one.json", "two.json"]', ["one.json", "two.json"])]
)
def test_session_environment_normalizes_paths(monkeypatch, value, expected):
    monkeypatch.setenv("BROWSER_STORAGE_STATE", value)
    cfg = Settings(_env_file=None)
    assert cfg.browser_storage_state == expected
    assert Settings.model_validate(cfg.model_dump()).browser_storage_state == expected


def test_session_string_config_stays_compatible():
    assert Settings(_env_file=None, browser_storage_state="old.json").browser_storage_state == ["old.json"]
