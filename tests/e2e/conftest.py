"""Shared fixtures for e2e tests — real network, real storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from crawlme.config import Settings


@pytest.fixture
def e2e_settings() -> Settings:
    """Settings pointed at results/ — Storage.create() nests timestamped subdirs."""
    return Settings(
        result_dir=Path("results"),
        ignore_robots=True,
        fetch_concurrency=2,
        fetch_timeout_connect=15.0,
        fetch_timeout_read=30.0,
        fetch_max_retries=2,
        log_level="DEBUG",
        log_format="console",
    )
