from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crawlme.cli import main


def test_run_help(capsys):
    """crawl with no args should print help."""
    with patch("sys.argv", ["crawl"]), pytest.raises(SystemExit):
        main()
    captured = capsys.readouterr()
    assert "usage" in captured.out or "usage" in captured.err


def test_run_prints_prompt(capsys):
    """crawl run <prompt> should not crash."""
    with patch("sys.argv", ["crawl", "run", "test prompt", "--seeds", "https://example.com"]):
        with patch("crawlme.cli.CrawlScheduler") as mock_sched_cls:
            mock_sched = MagicMock()
            mock_sched._storage = MagicMock()
            mock_sched._storage.start = AsyncMock()
            mock_sched._frontier = MagicMock()
            mock_sched._frontier.push_batch = AsyncMock()
            mock_sched._counters = {"pages_fetched": 0}
            mock_sched.run = AsyncMock()
            mock_sched_cls.return_value = mock_sched

            # Don't actually do I/O.
            try:
                main()
            except SystemExit:
                pass

    captured = capsys.readouterr()
    assert "test prompt" in captured.out
