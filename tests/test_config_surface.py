"""The config surface must not promise what the code ignores.

Two ways it rots. A knob documented in .env.example that nothing reads
is worse than an undocumented one: it is followed, has no effect, and
says nothing. A knob with both a flag and an env line leaves the reader
guessing which wins.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from crawlme.config import Settings

_ROOT = Path(__file__).resolve().parents[1]
_ENV_EXAMPLE = _ROOT / ".env.example"
_SRC = _ROOT / "src" / "crawlme"

# Read through the settings object rather than by field name.
_LOG_LEVEL_IS_A_DOCUMENTED_EXCEPTION = {"log_level"}


def _documented() -> list[str]:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    return [m.lower() for m in re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M)]


def _sources() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _SRC.rglob("*.py"))


@pytest.mark.parametrize("name", _documented())
def test_knob_is_real(name: str) -> None:
    assert name in Settings.model_fields, f"{name.upper()} is documented but is not a setting"


@pytest.mark.parametrize("name", _documented())
def test_knob_is_read(name: str) -> None:
    """Seven of these were shadowed by module constants and did nothing."""
    if name in _LOG_LEVEL_IS_A_DOCUMENTED_EXCEPTION:
        return
    src = _sources()
    assert re.search(rf"\.{re.escape(name)}\b", src), f"{name.upper()} is documented but nothing reads it"


def _visible_flags() -> set[str]:
    """Flag names that --help actually shows.

    A flag kept only so an older command line still runs is hidden with
    argparse.SUPPRESS and is not part of the documented surface, so it
    does not compete with an env line for the same knob.
    """
    text = (_SRC / "cli" / "__init__.py").read_text(encoding="utf-8")
    out: set[str] = set()
    for chunk in text.split("add_argument(")[1:]:
        body = chunk.split("add_argument(")[0]
        if "argparse.SUPPRESS" in body:
            continue
        out.update(re.findall(r'"--([a-z][a-z-]*)"', body))
    return out


def test_knob_once() -> None:
    """A flag and an env line for the same knob leave the reader guessing."""
    flags = _visible_flags()
    both = {n for n in _documented() if n.replace("_", "-") in flags} - _LOG_LEVEL_IS_A_DOCUMENTED_EXCEPTION
    assert not both, f"documented as both a flag and an env var: {sorted(both)}"


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
