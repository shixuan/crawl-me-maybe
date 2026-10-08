"""Settings-backed options use defaults, .env, environment, then CLI overrides."""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from crawlme.digest.fetcher.base import DEFAULT_UA


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Per-run settings
    result_dir: Path = Path("results")
    ignore_robots: bool = False
    # Retain LLM rejections at low priority; deterministic filters still apply.
    recall: bool = False
    # Optional per-page relevance and field extraction; disabled without credentials.
    analysis_enabled: bool = True
    # Maximum page-text characters sent to the analyzer.
    analyzer_max_chars: int = 3000

    # LLM stages are absent without a key or base URL.
    llm_model: str = ""  # "" = provider default (openai/gpt-4o-mini)
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_concurrency: int = 2
    # Response token ceiling, including reasoning tokens where applicable.
    llm_max_output_tokens: int = 16384
    # Split ranking batches at this text limit without truncating candidates.
    llm_max_batch_chars: int = 12_000
    # Per-stage effort; empty leaves the provider default.
    llm_rank_reasoning_effort: str = ""
    llm_analyze_reasoning_effort: str = ""
    llm_goal_reasoning_effort: str = ""
    llm_seeds_reasoning_effort: str = ""
    llm_dedup_reasoning_effort: str = "off"
    # Oversized dedup input is left ungrouped, never truncated.
    llm_dedup_max_chars: int = 100_000

    # --- Fetch ---
    fetch_concurrency: int = 6
    fetch_timeout_connect: float = 10.0
    fetch_timeout_read: float = 30.0
    fetch_max_retries: int = 3
    # "http" dispatches per platform; "browser" renders every candidate.
    fetcher: str = "http"
    # Playwright storage-state path; empty uses an anonymous context.
    browser_storage_state: str = ""
    # Per-page byte cap for sub-responses held in memory before storage.
    browser_max_payload_bytes: int = 8 * 1024 * 1024
    # Maximum scrolls on platforms that request scrolling.
    feed_scrolls: int = 4
    user_agents: list[str] = [DEFAULT_UA]

    # Timeout for each extraction or link-harvesting operation, in seconds.
    extract_timeout: float = 120.0

    # --- Frontier ---
    candidate_buffer_size: int = 2_000

    # Proposal count is clamped between these bounds.
    expand_seeds: bool = Field(default=False, validation_alias=AliasChoices("expand_seeds", "enhance_seeds"))
    expand_seeds_min: int = Field(default=4, validation_alias=AliasChoices("expand_seeds_min", "enhance_seeds_min"))
    expand_seeds_max: int = Field(default=12, validation_alias=AliasChoices("expand_seeds_max", "enhance_seeds_max"))

    # --- Logging ---
    # DEBUG | INFO | WARNING | ERROR | CRITICAL | OFF
    log_level: str = "INFO"
    log_format: str = "console"  # console | json
