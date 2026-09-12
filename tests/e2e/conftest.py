"""Fixtures for live-network tests, automatically marked e2e by directory.

Hermetic pipeline tests belong in tests/smoke/."""

from __future__ import annotations

from pathlib import Path

import pytest

from crawlme.config import Settings


@pytest.fixture
def e2e_settings() -> Settings:
    """Live fetching with robots bypassed and LLM stages disabled."""
    return Settings(
        fetch_concurrency=2,
        fetch_timeout_connect=15.0,
        fetch_timeout_read=30.0,
        fetch_max_retries=2,
        log_level="DEBUG",
        log_format="console",
        ignore_robots=True,
        embedding_provider="",
        # Keep live tests independent of LLM services.
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
