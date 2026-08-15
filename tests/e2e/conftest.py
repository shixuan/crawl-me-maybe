"""Shared fixtures for e2e tests (real network, real storage)."""

from __future__ import annotations

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
