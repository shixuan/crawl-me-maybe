from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from crawlme.config import Settings


@pytest.fixture
def tmp_data_dir() -> Path:
    """Create a temporary data directory for tests."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / "raw").mkdir(exist_ok=True)
        (p / "db").mkdir(exist_ok=True)
        yield p


@pytest.fixture
def test_settings(tmp_data_dir: Path) -> Settings:
    """Settings pointed at a temp directory."""
    return Settings(
        data_dir=tmp_data_dir,
        raw_dir=tmp_data_dir / "raw",
        db_path=tmp_data_dir / "db" / "test.db",
        ignore_robots=True,
    )
