"""Shared fixtures for e2e tests (real network, real storage).

Everything in this directory is assumed to hit the network, and is marked
`e2e` automatically so CI's `-m "not e2e"` filter excludes it.

The auto-marking is the point. An opt-in marker fails silently: a new
network test that forgets `pytestmark` just starts running on every push
and turns CI flaky for reasons that have nothing to do with the commit.
Marking by location inverts that, so forgetting is the safe direction.

Hermetic tests that want to run in CI belong in tests/smoke/.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crawlme.config import Settings


@pytest.fixture
def e2e_settings() -> Settings:
    """Settings for e2e runs. result dir defaults to results/, the
    real layout e2e tests want; robots bypassed, embedding off."""
    return Settings(
        fetch_concurrency=2,
        fetch_timeout_connect=15.0,
        fetch_timeout_read=30.0,
        fetch_max_retries=2,
        log_level="DEBUG",
        log_format="console",
        ignore_robots=True,
        embedding_provider="",
        # E2E crawls are network tests for FETCHING, not for LLM
        # stages: keep the factory-built feedback subsystem inert.
        llm_api_key="",
        llm_base_url="",
    )


_HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything collected from this directory as `e2e`.

    The path check is not optional: pytest hands this hook the whole
    session's items no matter which conftest defines it, so an unfiltered
    loop marks the entire suite and CI silently runs nothing.
    """
    for item in items:
        if _HERE in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.e2e)
