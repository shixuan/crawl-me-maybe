import subprocess
import sys

import pytest

_PACKAGES = ("urls", "goals", "runtime", "pioneer", "digest", "analysis", "discovery", "platforms", "dedup")


@pytest.mark.parametrize("first", ("config", *_PACKAGES))
def test_public_imports(first):
    script = """
import importlib
import sys

first, *packages = sys.argv[1:]
initial = importlib.import_module(f"crawlme.{first}")
for name in getattr(initial, "__all__", ()):
    getattr(initial, name)
for package in packages:
    module = importlib.import_module(f"crawlme.{package}")
    for name in module.__all__:
        getattr(module, name)
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, first, *_PACKAGES], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr


def test_goals_import_is_light():
    script = """
import sys
from crawlme.goals import Goal, spec_fields
assert Goal(prompt="find events").prompt == "find events"
assert spec_fields(None) == {}
assert "crawlme.goals.enhancer" not in sys.modules
assert "crawlme.llm" not in sys.modules
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
