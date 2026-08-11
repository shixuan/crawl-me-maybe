from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Paths ---
    result_dir: Path = Path("results")

    # --- LLM ---
    llm_model: str = "openai/gpt-4o-mini"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_concurrency: int = 2

    # --- Fetch ---
    fetch_concurrency: int = 6
    fetch_timeout_connect: float = 10.0
    fetch_timeout_read: float = 30.0
    fetch_max_retries: int = 3
    user_agents: list[str] = [
        "crawl-me-maybe/0.1 (research crawler; +https://github.com/crawl-me-maybe)",
    ]

    # --- Extract ---
    # Timeout for trafilatura extraction + bs4 link parsing (per page).
    # Trafilatura can degrade to O(n²) or worse on pathological HTML
    # (e.g. 6MB wikidata structured-data pages, giant ad-heavy news sites).
    # This is a safety valve, not a content filter — a healthy page under
    # a few MB should complete in <10 s.  Raise this if targeting
    # deliberately large / rich pages.  Future ideas: per-domain tuning,
    # streaming extraction, or extractor-level cancellation.
    extract_timeout: float = 120.0

    # --- Frontier ---
    candidate_buffer_size: int = 2_000
    rank_batch_size: int = 100
    rank_cooldown_sec: float = 30.0
    checkpoint_interval: int = 10
    priority_aging_window: float = 600.0

    # --- Budget defaults ---
    default_max_pages: int = 500
    default_max_tokens: int = 2_000_000
    default_max_duration_sec: int = 3600
    default_domain_budget: int = 50

    # --- Robots ---
    ignore_robots: bool = False
    robots_ttl_hours: int = 24
    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown_min: int = 10

    # --- Logging ---
    # DEBUG | INFO | WARNING | ERROR | CRITICAL | OFF
    log_level: str = "INFO"
    log_format: str = "json"  # json | console
