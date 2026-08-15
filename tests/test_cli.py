from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crawlme.cli import main
from crawlme.pioneer.goal_enhancer import EnhancedGoal, GoalEnhancer
from crawlme.pioneer.ranker.llm import LLMRanker
from crawlme.state.context import CrawlCounters


@pytest.fixture(autouse=True)
def _inert_goal_enhancer(monkeypatch):
    """Keep CLI tests hermetic: never touch a real LLM, whatever the
    developer's .env says."""

    def _inert(cls, settings, *, budget=None):
        return GoalEnhancer(None)

    monkeypatch.setattr(GoalEnhancer, "from_settings", classmethod(_inert))


@pytest.fixture(autouse=True)
def _inert_llm_ranker(monkeypatch):
    """Same for the LLM ranking stage: skip it in CLI tests."""

    def _inert(cls, settings, *, budget=None):
        return None

    monkeypatch.setattr(LLMRanker, "from_settings", classmethod(_inert))


def test_run_help(capsys):
    """crawl with no args should print help."""
    with patch("sys.argv", ["crawl"]), pytest.raises(SystemExit):
        main()
    captured = capsys.readouterr()
    assert "usage" in captured.out or "usage" in captured.err


def test_run_prints_prompt(caplog):
    """crawl run <prompt> should log task info via logging."""
    import logging

    with patch("sys.argv", ["crawl", "run", "test prompt", "--seeds", "https://example.com"]):
        with patch("crawlme.cli.create_scheduler") as mock_factory:
            mock_sched = MagicMock()
            mock_sched.ingest_seeds = AsyncMock()
            mock_sched._counters = CrawlCounters()
            mock_sched.run = AsyncMock()
            mock_factory.return_value = mock_sched

            # setup_logging uses Settings() which defaults to OFF.
            # Force the root logger to accept INFO so caplog captures it.
            logging.getLogger().setLevel(logging.INFO)
            with caplog.at_level(logging.INFO, logger="crawlme.cli"):
                # _cmd_run force-reconfigures logging, which would wipe the
                # caplog handler, so stub it out for this test.
                with patch("crawlme.cli.setup_logging"):
                    try:
                        main()
                    except SystemExit:
                        pass

    assert "test prompt" in caplog.text


def _capturing_factory(captured: dict):
    """Factory stub that records the Settings / goal / overrides it receives."""

    def _capture(cfg, goal=None, **overrides):
        captured["cfg"] = cfg
        captured["goal"] = goal
        captured["overrides"] = overrides
        sched = MagicMock()
        sched.ingest_seeds = AsyncMock()
        sched._counters = CrawlCounters()
        sched.run = AsyncMock()
        return sched

    return _capture


def test_run_flags_override_settings(tmp_path):
    """Per-run flags override Settings; goal budgets land in CrawlGoal."""
    captured: dict = {}
    fake_results = tmp_path / "fake-results"

    argv = [
        "crawl",
        "run",
        "test prompt",
        "--seeds",
        "https://example.com",
        "--embedding",
        "api",
        "--embedding-model",
        "jina-embeddings-v3",
        "--ignore-robots",
        "--domain-budget",
        "7",
        "--analyzer-max-chars",
        "3000",
        "--log-level",
        "WARNING",
        "--max-pages",
        "42",
        "--result-dir",
        str(fake_results),
    ]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass

    cfg = captured["cfg"]
    goal = captured["goal"]
    # Flags -> Settings
    assert cfg.embedding_provider == "api"
    assert cfg.embedding_model == "jina-embeddings-v3"
    assert cfg.ignore_robots is True
    assert str(cfg.result_dir) == str(fake_results)
    assert cfg.log_level == "WARNING"
    assert cfg.analyzer_max_chars == 3000
    # Flags -> CrawlGoal
    assert goal.max_pages == 42
    assert goal.domain_budget == 7


def test_run_flags_left_off_keep_env_defaults(monkeypatch):
    """Without flags, defaults apply. Embedding is ON (local) out of the box.

    Env vars are pinned explicitly: they outrank the developer's .env
    file, so this test is deterministic regardless of local config.
    """
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("IGNORE_ROBOTS", "false")

    captured: dict = {}
    with patch("sys.argv", ["crawl", "run", "test prompt", "--seeds", "https://example.com"]):
        with patch("crawlme.cli.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass

    cfg = captured["cfg"]
    goal = captured["goal"]
    assert cfg.embedding_provider == "local"  # full pipeline by default
    assert cfg.ignore_robots is False
    assert str(cfg.result_dir) == "results"
    assert cfg.log_level == "INFO"
    assert goal.max_pages == 500
    assert goal.domain_budget == 50


def test_run_applies_enhanced_goal(monkeypatch):
    """When the enhancer produces fields, they land on the goal."""
    captured: dict = {}

    class _StubEnhancer:
        @classmethod
        def from_settings(cls, settings, *, budget=None):
            return cls()

        async def enhance(self, goal):
            return EnhancedGoal(statement="Find ML papers", keywords=["ml", "papers"], since=None)

    monkeypatch.setattr("crawlme.cli.GoalEnhancer", _StubEnhancer)
    with patch("sys.argv", ["crawl", "run", "test prompt", "--seeds", "https://example.com"]):
        with patch("crawlme.cli.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass

    goal = captured["goal"]
    assert goal.goal_statement == "Find ML papers"
    assert goal.keywords == ["ml", "papers"]


def test_run_embedding_off_flag():
    """--embedding off opts out of semantic ranking."""
    captured: dict = {}
    argv = ["crawl", "run", "test prompt", "--seeds", "https://example.com", "--embedding", "off"]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["cfg"].embedding_provider == ""


def test_run_flag_beats_env_twin(monkeypatch):
    """When both env and flag are given for the same knob, the flag wins."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "api")
    captured: dict = {}
    argv = ["crawl", "run", "test prompt", "--seeds", "https://example.com", "--embedding", "off"]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["cfg"].embedding_provider == ""


def test_run_wires_llm_ranker_into_factory(monkeypatch):
    """A configured LLM ranker is passed to the scheduler factory."""
    captured: dict = {}
    sentinel = object()

    def _fake_from_settings(cls, settings, *, budget=None):
        return sentinel

    monkeypatch.setattr("crawlme.cli.LLMRanker", type("_Stub", (), {"from_settings": classmethod(_fake_from_settings)}))
    with patch("sys.argv", ["crawl", "run", "test prompt", "--seeds", "https://example.com"]):
        with patch("crawlme.cli.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass

    assert captured["overrides"]["llm_ranker"] is sentinel


def test_run_feedback_off_flag():
    """--feedback off disables the whole subsystem via Settings."""
    captured: dict = {}
    argv = ["crawl", "run", "test prompt", "--seeds", "https://example.com", "--feedback", "off"]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["cfg"].feedback_enabled is False


def test_run_feedback_defaults_on():
    """Without the flag the subsystem stays enabled (default True)."""
    captured: dict = {}
    with patch("sys.argv", ["crawl", "run", "test prompt", "--seeds", "https://example.com"]):
        with patch("crawlme.cli.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["cfg"].feedback_enabled is True


def test_run_binds_budget_sink_to_scheduler(monkeypatch):
    """The shared token budget's sink reaches the scheduler, which is
    what makes the BUDGET_TOKENS stop condition see LLM usage."""
    from crawlme.llm import TokenBudget

    note = object()
    recorded: list = []

    def _capture(cfg, goal=None, **overrides):
        sched = MagicMock()
        sched.ingest_seeds = AsyncMock()
        sched._counters = CrawlCounters()
        sched.run = AsyncMock()
        sched.note_tokens_used = note
        return sched

    monkeypatch.setattr(TokenBudget, "bind_sink", lambda self, sink: recorded.append(sink))
    with patch("sys.argv", ["crawl", "run", "test prompt", "--seeds", "https://example.com"]):
        with patch("crawlme.cli.create_scheduler", side_effect=_capture):
            try:
                main()
            except SystemExit:
                pass

    assert recorded == [note]


def test_run_prints_end_of_run_summary(capsys):
    """After a run, the terminal report shows the numbers that matter."""

    def _capture(cfg, goal=None, **overrides):
        sched = MagicMock()
        sched.ingest_seeds = AsyncMock()
        sched._counters = CrawlCounters(pages_fetched=5, tokens_used=1234)
        sched.run = AsyncMock()
        sched.summary = lambda: {
            "pages_fetched": 5,
            "tokens_used": 1234,
            "candidates_discovered": 100,
            "candidates_ranked": 30,
            "fetch_errors": 1,
            "analyses": {"RELEVANT": 2, "IRRELEVANT": 1},
            "duration_sec": 7.5,
            "embedding_cache_hits": 3,
            "embedding_cache_misses": 2,
        }
        return sched

    with patch("sys.argv", ["crawl", "run", "test prompt", "--seeds", "https://example.com"]):
        with patch("crawlme.cli.create_scheduler", side_effect=_capture):
            try:
                main()
            except SystemExit:
                pass

    out = capsys.readouterr().out
    assert "crawl finished" in out
    assert "5 fetched" in out
    assert "100 links discovered" in out
    assert "2 RELEVANT" in out
    assert "7.5s" in out
