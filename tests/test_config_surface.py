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

#: Read through the settings object rather than by field name.
_LOG_LEVEL_IS_A_DOCUMENTED_EXCEPTION = {"log_level"}


def _documented() -> list[str]:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    return [m.lower() for m in re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M)]


def _sources() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _SRC.rglob("*.py"))


@pytest.mark.parametrize("name", _documented())
def test_every_documented_knob_is_a_real_setting(name: str) -> None:
    assert name in Settings.model_fields, f"{name.upper()} is documented but is not a setting"


@pytest.mark.parametrize("name", _documented())
def test_every_documented_knob_is_read_somewhere(name: str) -> None:
    """Seven of these were shadowed by module constants and did nothing."""
    if name in _LOG_LEVEL_IS_A_DOCUMENTED_EXCEPTION:
        return
    src = _sources()
    assert re.search(rf"\.{re.escape(name)}\b", src), f"{name.upper()} is documented but nothing reads it"


def test_a_setting_is_documented_in_one_place_only() -> None:
    """A flag and an env line for the same knob leave the reader guessing."""
    flags = set(re.findall(r'"--([a-z][a-z-]*)"', (_SRC / "cli" / "__init__.py").read_text(encoding="utf-8")))
    both = {n for n in _documented() if n.replace("_", "-") in flags} - _LOG_LEVEL_IS_A_DOCUMENTED_EXCEPTION
    assert not both, f"documented as both a flag and an env var: {sorted(both)}"
