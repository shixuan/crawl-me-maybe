"""Configuration.

One Settings class reads env vars / .env for every knob.  CLI flags
override it at runtime, so the effective priority is:

    defaults  ->  .env  ->  env vars  ->  CLI flags

Documentation discipline: `.env.example` advertises only the set-once
knobs (secrets, timeouts, deep tuning).  The per-run knobs (result_dir,
ignore_robots, embedding_*, log_level) also exist here so flags can
override them, but their env twins are deliberately undocumented —
when both are given, the flag wins.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -: Per-run knobs (flags are the documented entry; env twins exist
    #    mechanically but are not advertised in .env.example) ---------
    result_dir: Path = Path("results")
    ignore_robots: bool = False
    # "local" default: the full pipeline (rule + embedding) runs out of
    # the box.  "" (--embedding off) = rule-only v0.1 behavior.
    embedding_provider: str = "local"  # local | api | ""
    embedding_model: str = ""  # "" = provider default

    # -: LLM (v0.2+) ---
    llm_model: str = "openai/gpt-4o-mini"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_concurrency: int = 2

    # -: Embedding ---
    # Credentials for the api provider (--embedding api).  Keys are
    # secrets: env vars only, never flags.
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_keep: int = 60

    # -: Fetch ---
    fetch_concurrency: int = 6
    fetch_timeout_connect: float = 10.0
    fetch_timeout_read: float = 30.0
    fetch_max_retries: int = 3
    user_agents: list[str] = [
        "crawl-me-maybe/0.1 (research crawler; +https://github.com/crawl-me-maybe)",
    ]

    # -: Extract ---
    # Timeout for trafilatura extraction + bs4 link parsing (per page).
    # Trafilatura can degrade to O(n^2) or worse on pathological HTML
    # (e.g. 6MB wikidata structured-data pages, giant ad-heavy news sites).
    # This is a safety valve, not a content filter: a healthy page under
    # a few MB should complete in <10 s.  Raise this if targeting
    # deliberately large / rich pages.
    extract_timeout: float = 120.0

    # -: Frontier ---
    candidate_buffer_size: int = 2_000
    rank_batch_size: int = 100
    rank_cooldown_sec: float = 30.0
    checkpoint_interval: int = 10
    priority_aging_window: float = 600.0

    # -: Robots ---
    robots_ttl_hours: int = 24
    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown_min: int = 10

    # -: Logging ---
    # DEBUG | INFO | WARNING | ERROR | CRITICAL | OFF
    # Documented dual knob: the --log-level flag overrides this default.
    log_level: str = "INFO"
    log_format: str = "json"  # json | console
