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
    data_dir: Path = Path("data")
    raw_dir: Path = Path("data/raw")
    db_path: Path = Path("data/db/crawl.db")

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
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"),
    ]

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
    log_level: str = "OFF"
    log_format: str = "json"  # json | console
