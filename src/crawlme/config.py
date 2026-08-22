"""Configuration.

One Settings class reads env vars / .env for every knob.  CLI flags
override it at runtime, so the effective priority is:

    defaults  ->  .env  ->  env vars  ->  CLI flags

Documentation discipline: `.env.example` advertises only the set-once
knobs (secrets, timeouts, deep tuning).  The per-run knobs (result_dir,
ignore_robots, log_level) also exist here so flags can
override them, but their env twins are deliberately undocumented.
When both are given, the flag wins.
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
    # Trade tokens for coverage: no stage removes a candidate, it only
    # ranks it last, and the page budget decides where to stop.  Off by
    # default because a link graph without a hard filter grows without
    # bound; a feed is finite, so the trade is available there.
    recall: bool = False
    # The analysis stage: one LLM call per fetched page, returning a
    # verdict, the fields the goal asked for, and the evidence behind
    # both.  On by default, degrades without credentials; --analysis
    # off disables it for a clean baseline.
    analysis_enabled: bool = True
    # Page text sent to the analyzer per page, in characters.  The
    # dominant analyzer cost driver.  Set to 3000 by the 10-replicate
    # benchmark (benchmark/feedback/): on research-style tasks the
    # 6000-char window was actively worse than no feedback at all,
    # while 3000 won both precision and single-run recall.
    analyzer_max_chars: int = 3000

    # -: LLM (v0.2+) ---
    # On by default.  Degrades automatically: without a key and without
    # a base url the LLM stages are skipped at wiring time, and runtime
    # failures fall back to rule scoring.
    llm_model: str = ""  # "" = provider default (openai/gpt-4o-mini)
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_concurrency: int = 2
    # Ceiling on one response, and on a reasoning model the thinking is
    # spent out of it before any answer is written.  Too low and the
    # reply comes back empty or half-finished, which reads like a broken
    # parser rather than a budget.  A ceiling is not a cost: only tokens
    # actually generated are billed, so headroom is free until used.
    llm_max_output_tokens: int = 8192
    # How much candidate text one ranking call may carry.  Candidates are
    # never cut to fit: a batch that would exceed this is split into more
    # calls, because a post whose only relevant line sits past a cut is
    # rejected for not containing what was cut off.  Raise it for a model
    # with a larger context, lower it if a provider rejects the request.
    llm_max_batch_chars: int = 12_000
    # How hard the model thinks before answering, for models that think.
    # Empty sends nothing and takes the provider's default, which is what
    # every run before this one paid for: measured on one crawl, 84% of
    # output tokens were thinking, and thinking is billed as output and
    # then discarded.  Values are the provider's ("minimal", "low",
    # "medium", "high", and on some providers "none"), passed through
    # rather than validated here, because the vocabulary is theirs.
    llm_reasoning_effort: str = ""
    # The same, for the ranking stage alone.  Empty falls back to the
    # setting above.  It gets its own knob because it is the one stage
    # the trade was measured on: over 114 candidates, thinking bought
    # +0.069 AUC and cost nine times the output, and the value it landed
    # on without thinking (0.903) is what the shipped ranker was already
    # scoring with it (0.914).
    llm_rank_reasoning_effort: str = ""

    # -: Fetch ---
    fetch_concurrency: int = 6
    fetch_timeout_connect: float = 10.0
    fetch_timeout_read: float = 30.0
    fetch_max_retries: int = 3
    # "http" is plain httpx; "browser" renders with Playwright, which is
    # what a JS-built timeline or a login-walled platform needs.
    fetcher: str = "http"
    # Path to a storage_state JSON the user exports themselves.  Empty
    # means an anonymous browser.  Secrets stay out of flags: this is a
    # path, and the file itself never enters the repo.
    browser_storage_state: str = ""
    # Ceiling on what one page load may keep of its own sub-responses.
    # They are held in memory before they reach disk, so this is the
    # difference between a heavy page and an out-of-memory machine.
    browser_max_payload_bytes: int = 8 * 1024 * 1024
    # How many times a feed listing is asked for more of itself.  A
    # listing hands out one screen, so a window measured in weeks is
    # answered with the dozen most recent posts unless someone keeps
    # asking.  Each scroll is one more request the page makes, so this is
    # also the knob that trades coverage against how much a platform
    # sees of the crawl.  Ignored outside feed mode: a link graph has
    # nothing below the fold worth waiting for.
    feed_scrolls: int = 4
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

    # -: Logging ---
    # DEBUG | INFO | WARNING | ERROR | CRITICAL | OFF
    # Documented dual knob: the --log-level flag overrides this default.
    log_level: str = "INFO"
    log_format: str = "json"  # json | console
