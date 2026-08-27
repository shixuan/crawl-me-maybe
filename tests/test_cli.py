from __future__ import annotations

import argparse
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crawlme.cli import main
from crawlme.cli.run import _build_source
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
        with patch("crawlme.cli.run.create_scheduler") as mock_factory:
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
                with patch("crawlme.cli.run.setup_logging"):
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
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass

    cfg = captured["cfg"]
    goal = captured["goal"]
    # Flags -> Settings
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
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass

    cfg = captured["cfg"]
    goal = captured["goal"]
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

    monkeypatch.setattr("crawlme.cli.run.GoalEnhancer", _StubEnhancer)
    with patch("sys.argv", ["crawl", "run", "test prompt", "--seeds", "https://example.com"]):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass

    goal = captured["goal"]
    assert goal.goal_statement == "Find ML papers"
    assert goal.keywords == ["ml", "papers"]


def test_run_session_flag_reads_the_platform_through_the_browser(_installed, tmp_path):
    """Asking to crawl as someone and getting plain httpx would crawl
    the logged-out site and report it as the site.

    Asserted on where a candidate lands rather than on a settings
    value: the run is free to fetch a shop the analyser endorsed with
    plain HTTP, and does, because the cookies mean nothing there.
    """
    captured: dict = {}
    # A real file: the run refuses to start without one, deliberately.
    session = tmp_path / "state.json"
    session.write_text('{"cookies": [{"name": "s", "value": "x"}], "origins": []}')
    state = str(session)
    argv = ["crawl", "run", "test prompt", "--seeds", "https://example.com", "--session", state]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    from crawlme.digest.fetcher import PlaywrightFetcher
    from crawlme.scheduler.factory import _build_fetcher

    cfg = captured["cfg"]
    assert cfg.browser_storage_state == state
    fetcher = _build_fetcher(cfg)
    platform = fetcher._pick("https://www.instagram.com/someone/")
    assert isinstance(platform, PlaywrightFetcher)
    assert platform._storage_state == state
    assert not isinstance(fetcher._pick("https://a-shop.example.com/promo"), PlaywrightFetcher)


def test_run_wires_llm_ranker_into_factory(monkeypatch):
    """A configured LLM ranker is passed to the scheduler factory."""
    captured: dict = {}
    sentinel = object()

    def _fake_from_settings(cls, settings, *, budget=None):
        return sentinel

    monkeypatch.setattr(
        "crawlme.cli.run.LLMRanker", type("_Stub", (), {"from_settings": classmethod(_fake_from_settings)})
    )
    with patch("sys.argv", ["crawl", "run", "test prompt", "--seeds", "https://example.com"]):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass

    assert captured["overrides"]["llm_ranker"] is sentinel


def test_run_analysis_off_flag():
    """--analysis off disables the whole subsystem via Settings."""
    captured: dict = {}
    argv = ["crawl", "run", "test prompt", "--seeds", "https://example.com", "--analysis", "off"]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["cfg"].analysis_enabled is False


def test_run_analysis_defaults_on():
    """Without the flag the subsystem stays enabled (default True)."""
    captured: dict = {}
    with patch("sys.argv", ["crawl", "run", "test prompt", "--seeds", "https://example.com"]):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["cfg"].analysis_enabled is True


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
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capture):
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
        }
        return sched

    with patch("sys.argv", ["crawl", "run", "test prompt", "--seeds", "https://example.com"]):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capture):
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


# --since parsing (2.8) -------------------------------------------------


def test_parse_since_relative_window() -> None:
    from crawlme.cli.run import _parse_since

    cutoff = _parse_since("1 week")
    delta = datetime.datetime.now(datetime.timezone.utc) - cutoff
    assert 6.9 < delta.days + delta.seconds / 86400 < 7.1


def test_parse_since_absolute_date_is_utc_aware() -> None:
    from crawlme.cli.run import _parse_since

    cutoff = _parse_since("2026-08-01")
    assert cutoff.tzinfo is not None
    assert (cutoff.year, cutoff.month, cutoff.day) == (2026, 8, 1)


def test_parse_since_plural_and_singular_agree() -> None:
    from crawlme.cli.run import _parse_since

    assert (_parse_since("1 day") - _parse_since("1 days")).total_seconds() < 1


def test_parse_since_rejects_garbage() -> None:
    from crawlme.cli.run import _parse_since

    with pytest.raises(ValueError):
        _parse_since("whenever")


# where the entry points come from ---------------------------------------


def _source_for(argv_tail: list[str], tmp_path):
    captured: dict = {}
    argv = ["crawl", "run", "test prompt", "--result-dir", str(tmp_path), *argv_tail]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            with patch("crawlme.cli.run._build_source", side_effect=_build_source) as spy:
                try:
                    main()
                except SystemExit as exc:
                    return exc.code, spy
    return 0, spy


def test_seeds_file_needs_no_mode_flag(tmp_path):
    seeds = tmp_path / "seeds.json"
    seeds.write_text('["https://example.com/a"]')
    code, _ = _source_for(["--seeds", str(seeds)], tmp_path)
    assert code in (0, None)


def test_recall_flag_reaches_settings(tmp_path):
    """Trading tokens for coverage is a per-run choice, so it is a flag."""
    captured: dict = {}
    argv = ["crawl", "run", "p", "--seeds", "https://example.com", "--recall", "--result-dir", str(tmp_path)]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["cfg"].recall is True


def test_recall_is_off_unless_asked(tmp_path):
    captured: dict = {}
    argv = ["crawl", "run", "p", "--seeds", "https://example.com", "--result-dir", str(tmp_path)]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["cfg"].recall is False


def test_feed_run_defaults_to_no_ceiling(_installed, tmp_path):
    """A session says this reads a platform, and there every candidate
    shares one host: a per-domain ceiling is a total."""
    captured: dict = {}
    argv = [
        "crawl",
        "run",
        "p",
        "--seeds",
        "https://instagram.com/a/",
        "--session",
        _session(tmp_path),
        "--result-dir",
        str(tmp_path),
    ]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["goal"].domain_budget == 0


def test_asking_for_a_domain_budget_still_applies_it(_installed, tmp_path):
    captured: dict = {}
    argv = [
        "crawl",
        "run",
        "p",
        "--seeds",
        "https://instagram.com/a/",
        "--session",
        _session(tmp_path),
        "--domain-budget",
        "5",
        "--result-dir",
        str(tmp_path),
    ]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["goal"].domain_budget == 5


def test_link_graph_keeps_its_ceiling(tmp_path):
    """One site can otherwise absorb a whole graph crawl."""
    captured: dict = {}
    argv = ["crawl", "run", "p", "--seeds", "https://example.com", "--result-dir", str(tmp_path)]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["goal"].domain_budget == 50


def test_result_target_reaches_the_goal(tmp_path):
    captured: dict = {}
    argv = [
        "crawl",
        "run",
        "p",
        "--seeds",
        "https://example.com",
        "--max-relevant",
        "50",
        "--result-dir",
        str(tmp_path),
    ]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["goal"].max_relevant == 50


def test_budgets_keep_their_older_spelling(tmp_path):
    """--max-pages is in every command line already written."""
    captured: dict = {}
    argv = ["crawl", "run", "p", "--seeds", "https://example.com", "--max-pages", "7", "--result-dir", str(tmp_path)]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["goal"].max_pages == 7


def _session(tmp_path) -> str:
    """A feed run refuses to start without one, so every feed test needs it."""
    f = tmp_path / "session.json"
    f.write_text('{"cookies": [{"name": "s", "value": "x"}], "origins": []}')
    return str(f)


@pytest.fixture
def _installed():
    """Say every optional extra is present, whatever this machine has.

    A session implies a browser, and a browser is an optional install
    that CI does not carry: without this, tests about flags reaching
    Settings would fail on the extras check instead, several steps
    before the thing they are about.
    """
    with patch("crawlme.cli.run.importlib.util.find_spec", return_value=object()):
        yield


# the session preflight ---------------------------------------------------


def test_a_missing_session_file_stops_before_the_crawl(tmp_path, capsys):
    """It used to raise inside the fetcher, several hundred pages in."""
    argv = [
        "crawl",
        "run",
        "p",
        "--seeds",
        "https://instagram.com/x/",
        "--session",
        str(tmp_path / "nope.json"),
    ]
    with patch("sys.argv", argv):
        with pytest.raises(SystemExit):
            main()
    err = capsys.readouterr().err
    assert "no session file" in err
    assert "crawl session" in err, "the message has to name the command that fixes it"


def test_seeds_on_a_walled_platform_are_refused_without_a_session(tmp_path, capsys):
    """The flag was never what decided this.

    Pasting profile URLs into --seeds and forgetting --feed walked
    straight past the old check and fetched login pages: six hundred
    kilobytes of markup holding nine characters of text, judged
    irrelevant, reported as a finished crawl of a quiet platform.
    """
    from crawlme.cli.run import _check_session

    args = argparse.Namespace(session=None, seeds="https://www.instagram.com/cafe/")
    with pytest.raises(SystemExit):
        _check_session(args)
    assert "crawling instagram needs a session" in capsys.readouterr().err


def test_seeds_in_a_file_are_read_for_the_same_check(tmp_path, capsys):
    """A list in a file is the same list, so it gets the same answer."""
    from crawlme.cli.run import _check_session

    f = tmp_path / "seeds.json"
    f.write_text('{"seeds": ["https://www.instagram.com/cafe/"]}')
    args = argparse.Namespace(session=None, seeds=str(f))
    with pytest.raises(SystemExit):
        _check_session(args)
    assert "instagram" in capsys.readouterr().err


def test_ordinary_seeds_are_not_bothered(tmp_path, capsys):
    from crawlme.cli.run import _check_session

    args = argparse.Namespace(session=None, seeds="https://example.com/a")
    _check_session(args)
    assert capsys.readouterr().err == ""


def test_a_feed_without_a_session_is_refused(capsys):
    """A warning here scrolls past, and the run spends a browser on it.

    Every fetch would land on the platform's login page, which reads as
    a platform with nothing on it.
    """
    from crawlme.cli.run import _check_session

    with pytest.raises(SystemExit):
        _check_session(argparse.Namespace(session=None, seeds="https://www.instagram.com/cafe/"))
    assert "crawl session" in capsys.readouterr().err


def test_a_link_graph_without_a_session_says_nothing(capsys):
    from crawlme.cli.run import _check_session

    _check_session(argparse.Namespace(session=None, seeds=None))
    assert capsys.readouterr().err == ""


def test_a_link_graph_is_not_told_to_make_a_feed_session(tmp_path, capsys):
    """It asked for a session and named a file that is not there, which
    is worth saying. The advice for making one is feed-shaped, so
    offering it here would point at a command that cannot serve it."""
    from crawlme.cli.run import _check_session

    with pytest.raises(SystemExit):
        _check_session(
            argparse.Namespace(
                session=str(tmp_path / "nope.json"),
                feed=None,
                seeds=None,
                seeds_file=None,
                source=None,
                source_path=None,
            )
        )
    err = capsys.readouterr().err
    assert "no session file" in err
    assert "crawl session" not in err


# optional installs -------------------------------------------------------


def _extras_args(**kw):
    base = {"seeds": None, "session": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_a_feed_flag_without_feedparser_is_refused(capsys):
    """It used to surface as an ImportError from inside seed discovery,
    naming a package the user never asked for."""
    from crawlme.cli.run import _check_extras
    from crawlme.config import Settings

    with patch("importlib.util.find_spec", return_value=None):
        with pytest.raises(SystemExit):
            _check_extras(Settings(), _extras_args(seeds="https://x/feed.xml"))
    err = capsys.readouterr().err
    assert "a feed among the seeds needs feedparser" in err
    assert "crawl-me-maybe[rss]" in err


def test_a_browser_run_without_playwright_is_refused(capsys):
    """By the first fetch the run directory exists and the goal has
    already cost an LLM call."""
    from crawlme.cli.run import _check_extras
    from crawlme.config import Settings

    with patch("importlib.util.find_spec", return_value=None):
        with pytest.raises(SystemExit):
            _check_extras(Settings(fetcher="browser"), _extras_args(session="s.json"))
    err = capsys.readouterr().err
    assert "--session needs playwright" in err
    assert "playwright install chromium" in err, "the package alone does not fetch a browser"


def test_a_link_graph_run_needs_neither(capsys):
    from crawlme.cli.run import _check_extras
    from crawlme.config import Settings

    with patch("importlib.util.find_spec", return_value=None):
        _check_extras(Settings(), _extras_args())
    assert capsys.readouterr().err == ""


def test_an_installed_extra_is_not_complained_about(capsys):
    from crawlme.cli.run import _check_extras
    from crawlme.config import Settings

    with patch("importlib.util.find_spec", return_value=object()):
        _check_extras(Settings(fetcher="browser"), _extras_args(seeds="https://x/feed.xml", session="s"))
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("BUDGET_PAGES", 0),
        ("FRONTIER_DRAINED", 0),
        ("MAX_RELEVANT", 0),
        ("DIMINISHING_RETURNS", 0),
        ("LOGIN_REQUIRED", 1),
        ("RATE_LIMITED", 1),
        ("FATAL", 1),
        ("FRONTIER_DRAINED+DOMAIN_BUDGET", 0),
        ("BUDGET_PAGES+LOGIN_REQUIRED", 1),
        (None, 0),
        ("", 0),
    ],
)
def test_a_refused_crawl_exits_non_zero(reason, code):
    """A crawl the platform refused is not a crawl that found nothing.

    A scheduled job cannot tell the two apart from a zero exit code, and
    weekly that is the difference between "no new posts" and "the
    session expired a month ago".
    """
    from crawlme.cli.run import exit_code

    assert exit_code(reason) == code


@pytest.mark.parametrize(
    ("flags", "expected_depth"),
    [
        ([], 5),
        (["--depth-limit", "1"], 1),
        (["--depth-limit", "3"], 3),
    ],
)
def test_a_session_does_not_decide_how_deep_to_go(_installed, tmp_path, flags, expected_depth):
    """Two levels held only while a platform run could not leave the
    platform.  It can now -- a listing, its posts, and a site an
    analyser endorsed off one is already three -- so a depth of 1 would
    drop the endorsement that is the whole way out.

    Where to stop is the user's to say, and unsaid means the ordinary
    default rather than a number the session picked for them.
    """
    session = tmp_path / "state.json"
    session.write_text('{"cookies": [{"name": "s", "value": "x"}], "origins": []}')
    captured: dict = {}
    argv = [
        "crawl",
        "run",
        "test prompt",
        "--seeds",
        "https://example.com",
        "--session",
        str(session),
        *flags,
    ]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["goal"].depth_limit == expected_depth


def test_a_session_still_lifts_the_per_domain_ceiling(_installed, tmp_path):
    """Every candidate on a platform shares one host, so a per-domain
    ceiling would be a ceiling on the whole crawl."""
    session = tmp_path / "state.json"
    session.write_text('{"cookies": [{"name": "s", "value": "x"}], "origins": []}')
    captured: dict = {}
    argv = ["crawl", "run", "p", "--seeds", "https://example.com", "--session", str(session)]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_scheduler", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["goal"].domain_budget == 0
