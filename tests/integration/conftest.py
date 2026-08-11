"""Shared fixtures for integration tests — real network, real storage.

Data is persisted under tests/integration/data/ so you can inspect results.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crawlme.config import Settings

DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture
def integration_settings() -> Settings:
    """Settings pointed at tests/integration/data/ for easy inspection."""
    (DATA_DIR / "raw").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "db").mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=DATA_DIR,
        raw_dir=DATA_DIR / "raw",
        db_path=DATA_DIR / "db" / "crawl.db",
        ignore_robots=True,
        fetch_concurrency=2,
        fetch_timeout_connect=15.0,
        fetch_timeout_read=30.0,
        fetch_max_retries=2,
        log_level="DEBUG",
        log_format="console",
    )
