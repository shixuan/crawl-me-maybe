from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crawlme.cli import main
from crawlme.pioneer.goal_enhancer import EnhancedGoal, GoalEnhancer
from crawlme.schemas import CrawlCounters


@pytest.fixture(autouse=True)
def _inert_goal_enhancer(monkeypatch):
    """Keep CLI tests hermetic: never touch a real LLM, whatever the
    developer's .env says."""

    def _inert(cls, settings, *, budget=None):
        return GoalEnhancer(None)

    monkeypatch.setattr(GoalEnhancer, "from_settings", classmethod(_inert))


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
    """Factory stub that records the Settings / goal it receives."""

    def _capture(cfg, goal=None, **overrides):
        captured["cfg"] = cfg
        captured["goal"] = goal
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
