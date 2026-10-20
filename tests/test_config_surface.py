"""Environment settings and compatibility precedence."""

from __future__ import annotations

import pytest

from crawlme.config import Settings


@pytest.mark.parametrize("prefix", ["EXPAND", "ENHANCE"])
@pytest.mark.parametrize("source", ["env", "dotenv"])
def test_seed_expansion_settings(prefix, source, tmp_path, monkeypatch):
    for name in ("EXPAND", "ENHANCE"):
        for suffix in ("", "_MIN", "_MAX"):
            monkeypatch.delenv(f"{name}_SEEDS{suffix}", raising=False)
    values = {f"{prefix}_SEEDS": "true", f"{prefix}_SEEDS_MIN": "2", f"{prefix}_SEEDS_MAX": "7"}
    env_file = tmp_path / ".env"
    if source == "env":
        for key, value in values.items():
            monkeypatch.setenv(key, value)
    else:
        env_file.write_text("\n".join(f"{key}={value}" for key, value in values.items()))
    cfg = Settings(_env_file=env_file, llm_api_key="", llm_base_url="", llm_model="")
    assert cfg.expand_seeds is True
    assert (cfg.expand_seeds_min, cfg.expand_seeds_max) == (2, 7)


def test_new_seed_setting_wins(monkeypatch):
    monkeypatch.setenv("EXPAND_SEEDS_MIN", "3")
    monkeypatch.setenv("ENHANCE_SEEDS_MIN", "9")
    cfg = Settings(_env_file=None, llm_api_key="", llm_base_url="", llm_model="")
    assert cfg.expand_seeds_min == 3
