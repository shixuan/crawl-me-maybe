from __future__ import annotations

import argparse
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crawlme.cli import main
from crawlme.errors import ErrorHandler
from crawlme.pioneer.goal_enhancer import EnhancedGoal, GoalEnhancer
from crawlme.pioneer.ranker import LLMRanker
from crawlme.pioneer.sources import create_source
from crawlme.runtime import Limits, Progress, RunState, Stats
from crawlme.scheduler.engine import create_engine
from crawlme.scheduler.pipelines.crawl import CrawlPipeline


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


@pytest.mark.parametrize("flags, expected", [([], "on"), (["--dedup", "on"], "on"), (["--dedup", "off"], "off")])
def test_dedup_flag(flags, expected):
    with (
        patch("sys.argv", ["crawl", "run", "test", *flags]),
        patch("crawlme.cli._dispatch", new_callable=AsyncMock) as dispatch,
    ):
        main()
    assert dispatch.call_args.args[0].dedup == expected


def _capturing_factory(captured: dict):
    """Factory stub that records the Settings / goal / overrides it receives."""

    def _capture(cfg, goal=None, **overrides):
        captured["cfg"] = cfg
        captured["goal"] = goal
        captured["overrides"] = overrides
        sched = MagicMock()
        from crawlme.scheduler.pipelines.factory import GoalEnhancer as Enhancer

        sched.error_handler = ErrorHandler()
        sched._goal_enhancer = Enhancer.from_settings(cfg)
        sched.startup = lambda *args, **kwargs: CrawlPipeline.startup(sched, *args, **kwargs)
        sched.ingest_seeds = AsyncMock()
        sched.expand_seeds = AsyncMock(return_value=[])
        sched.run_state = RunState(limits=Limits(), progress=Progress(), stats=Stats())
        sched.monitor = None
        sched.checkpoint = None
        sched.aclose = AsyncMock()
        sched.run = AsyncMock(side_effect=lambda input, context: captured.update(goal=input.goal))
        return sched

    return _capture


@pytest.mark.parametrize("flag", ["--expand-seeds", "--enhance-seeds"])
def test_seed_expansion_flag(flag):
    captured: dict = {}
    with (
        patch("sys.argv", ["crawl", "run", "test", "--seeds", "https://example.com", flag]),
        patch("crawlme.cli.run.create_crawl", side_effect=_capturing_factory(captured)),
    ):
        main()
    assert captured["cfg"].expand_seeds is True


def test_flags_win(tmp_path):
    """Per-run flags override Settings; goal budgets land in Goal."""
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
        with patch("crawlme.cli.run.create_crawl", side_effect=_capturing_factory(captured)):
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
    # Flags -> Goal
    assert goal.max_pages == 42
    assert goal.domain_budget == 7


def test_goal_enhanced(monkeypatch):
    """When the enhancer produces fields, they land on the goal."""
    captured: dict = {}

    class _StubEnhancer:
        @classmethod
        def from_settings(cls, settings, *, budget=None):
            return cls()

        async def enhance(self, goal):
            return EnhancedGoal(statement="Find ML papers", keywords=["ml", "papers"], since=None)

    monkeypatch.setattr("crawlme.scheduler.pipelines.factory.GoalEnhancer", _StubEnhancer)
    with patch("sys.argv", ["crawl", "run", "test prompt", "--seeds", "https://example.com"]):
        with patch("crawlme.cli.run.create_crawl", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass

    goal = captured["goal"]
    assert goal.goal_statement == "Find ML papers"
    assert goal.keywords == ["ml", "papers"]


def test_session_browser(_installed, tmp_path):
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
        with patch("crawlme.cli.run.create_crawl", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    from crawlme.digest import PlaywrightFetcher
    from crawlme.scheduler.pipelines.factory import _build_fetcher

    cfg = captured["cfg"]
    assert cfg.browser_storage_state == state
    fetcher = _build_fetcher(cfg)
    platform = fetcher._pick("https://www.instagram.com/someone/")
    assert isinstance(platform, PlaywrightFetcher)
    assert platform._storage_state == state
    assert not isinstance(fetcher._pick("https://a-shop.example.com/promo"), PlaywrightFetcher)


def test_analysis_off():
    """--analysis off disables the whole subsystem via Settings."""
    captured: dict = {}
    argv = ["crawl", "run", "test prompt", "--seeds", "https://example.com", "--analysis", "off"]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_crawl", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["cfg"].analysis_enabled is False


def test_prints_summary(capsys):
    """After a run, the terminal report shows the numbers that matter."""

    def _capture(cfg, goal=None, **overrides):
        sched = MagicMock()
        from crawlme.scheduler.pipelines.factory import GoalEnhancer as Enhancer

        sched.error_handler = ErrorHandler()
        sched._goal_enhancer = Enhancer.from_settings(cfg)
        sched.startup = lambda *args, **kwargs: CrawlPipeline.startup(sched, *args, **kwargs)
        sched.ingest_seeds = AsyncMock()
        sched.expand_seeds = AsyncMock(return_value=[])
        sched.run_state = RunState(limits=Limits(), progress=Progress(pages_fetched=5), stats=Stats())
        sched.monitor = None
        sched.checkpoint = None
        sched.aclose = AsyncMock()
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
        with patch("crawlme.cli.run.create_crawl", side_effect=_capture):
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


@pytest.mark.parametrize("log_format", ["console", "json"])
@pytest.mark.parametrize("log_level", ["INFO", "WARNING", "OFF"])
def test_summary_in_log(tmp_path, capsys, log_format, log_level):
    import asyncio
    import logging

    from crawlme.cli import _dispatch
    from crawlme.cli.report import make_report
    from crawlme.config import Settings
    from crawlme.llm import TokenBudget
    from crawlme.logging import setup_logging, to_file

    scheduler = MagicMock()
    scheduler.pipeline.summary.return_value = {
        "pages_fetched": 2,
        "seeds": {"seed": {"url": "https://example.com/", "funnel": (3, 2, 2, 2, 2, 1)}},
    }
    root = logging.getLogger()
    path = tmp_path / "log"
    with patch.object(root, "handlers", []), patch.object(root, "level", logging.INFO):
        setup_logging(Settings(log_level=log_level, log_format=log_format), force=True)
        to_file(str(path))
        try:
            budget = TokenBudget(limit=100)
            budget.record(12, 8, stage="dedup")
            report = make_report({**scheduler.pipeline.summary(), "state": "COMPLETED", "usage": budget.dump()})
            with patch("crawlme.cli.cmd_run", AsyncMock(return_value=report)):
                asyncio.run(_dispatch(argparse.Namespace(command="run")))
            captured = capsys.readouterr()
            saved = path.read_text()
            assert captured.out.strip() in saved
            assert "https://example.com/" in saved
            assert "dedup" in saved
            assert "12 in / 8 out" in saved
            assert saved.count("crawl finished") == 1
            assert captured.out.count("crawl finished") == 1
            assert "crawl finished" not in captured.err
        finally:
            for handler in root.handlers:
                handler.close()


# reading a moment off the command line ---------------------------------


def test_since_relative() -> None:
    from crawlme.cli.cutoff import read_cutoff

    cutoff = read_cutoff("1 week", flag="--since")
    delta = datetime.datetime.now(datetime.timezone.utc) - cutoff
    assert 6.9 < delta.days + delta.seconds / 86400 < 7.1


def test_since_absolute() -> None:
    from crawlme.cli.cutoff import read_cutoff

    cutoff = read_cutoff("2026-08-01", flag="--since")
    assert cutoff.tzinfo is not None
    assert (cutoff.year, cutoff.month, cutoff.day) == (2026, 8, 1)


def test_offset_turns_around() -> None:
    """The same words mean a week back for what was published and a week
    forward for what is still to come."""
    from crawlme.cli.cutoff import read_cutoff

    back = read_cutoff("1 week", flag="--since")
    ahead = read_cutoff("1 week", flag="--during", ahead=True)
    assert back < datetime.datetime.now(datetime.timezone.utc) < ahead
    assert 13.9 < (ahead - back).days + 0.1 < 14.2


def test_since_garbage() -> None:
    from crawlme.cli.cutoff import read_cutoff

    with pytest.raises(ValueError):
        read_cutoff("whenever", flag="--since")


# where the entry points come from ---------------------------------------


def _source_for(argv_tail: list[str], tmp_path):
    captured: dict = {}
    argv = ["crawl", "run", "test prompt", "--result-dir", str(tmp_path), *argv_tail]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_crawl", side_effect=_capturing_factory(captured)):
            with patch("crawlme.cli.run.create_source", side_effect=create_source) as spy:
                try:
                    main()
                except SystemExit as exc:
                    return exc.code, spy
    return 0, spy


def test_seeds_file(tmp_path):
    seeds = tmp_path / "seeds.json"
    seeds.write_text('["https://example.com/a"]')
    code, _ = _source_for(["--seeds", str(seeds)], tmp_path)
    assert code in (0, None)


def test_recall_flag(tmp_path):
    """Trading tokens for coverage is a per-run choice, so it is a flag."""
    captured: dict = {}
    argv = ["crawl", "run", "p", "--seeds", "https://example.com", "--recall", "--result-dir", str(tmp_path)]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_crawl", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["cfg"].recall is True


def test_feed_no_ceiling(_installed, tmp_path):
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
        with patch("crawlme.cli.run.create_crawl", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["goal"].domain_budget == 0


def test_budget_explicit(_installed, tmp_path):
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
        with patch("crawlme.cli.run.create_crawl", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["goal"].domain_budget == 5


def test_budget_aliases(tmp_path):
    """--max-pages is in every command line already written."""
    captured: dict = {}
    argv = ["crawl", "run", "p", "--seeds", "https://example.com", "--max-pages", "7", "--result-dir", str(tmp_path)]
    with patch("sys.argv", argv):
        with patch("crawlme.cli.run.create_crawl", side_effect=_capturing_factory(captured)):
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


def test_session_missing(tmp_path, capsys):
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


def test_walled_refused(tmp_path, capsys):
    """The flag was never what decided this.

    Pasting profile URLs into --seeds and forgetting --feed walked
    straight past the old check and fetched login pages: six hundred
    kilobytes of markup holding nine characters of text, judged
    irrelevant, reported as a finished crawl of a quiet platform.
    """
    from crawlme.cli.run import _check_session

    args = argparse.Namespace(session=None, seeds="https://www.instagram.com/cafe/")
    with pytest.raises(ValueError) as error:
        _check_session(args)
    assert "crawling instagram needs a session" in str(error.value)


def test_walled_in_file(tmp_path, capsys):
    """A list in a file is the same list, so it gets the same answer."""
    from crawlme.cli.run import _check_session

    f = tmp_path / "seeds.json"
    f.write_text('{"seeds": ["https://www.instagram.com/cafe/"]}')
    args = argparse.Namespace(session=None, seeds=str(f))
    with pytest.raises(ValueError) as error:
        _check_session(args)
    assert "instagram" in str(error.value)


def test_feed_needs_session(capsys):
    """A warning here scrolls past, and the run spends a browser on it.

    Every fetch would land on the platform's login page, which reads as
    a platform with nothing on it.
    """
    from crawlme.cli.run import _check_session

    with pytest.raises(ValueError) as error:
        _check_session(argparse.Namespace(session=None, seeds="https://www.instagram.com/cafe/"))
    assert "crawl session" in str(error.value)


def test_graph_no_hint(tmp_path, capsys):
    """It asked for a session and named a file that is not there, which
    is worth saying. The advice for making one is feed-shaped, so
    offering it here would point at a command that cannot serve it."""
    from crawlme.cli.run import _check_session

    with pytest.raises(ValueError) as error:
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
    err = str(error.value)
    assert "no session file" in err
    assert "crawl session" not in err


# optional installs -------------------------------------------------------


def _extras_args(**kw):
    base = {"seeds": None, "session": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_no_feedparser(capsys):
    """It used to surface as an ImportError from inside seed discovery,
    naming a package the user never asked for."""
    from crawlme.cli.run import _check_extras
    from crawlme.config import Settings

    with patch("importlib.util.find_spec", return_value=None):
        with pytest.raises(ValueError) as error:
            _check_extras(Settings(), _extras_args(seeds="https://x/feed.xml"))
    err = str(error.value)
    assert "a feed among the seeds needs feedparser" in err
    assert "crawl-me-maybe[rss]" in err


def test_no_playwright(capsys):
    """By the first fetch the run directory exists and the goal has
    already cost an LLM call."""
    from crawlme.cli.run import _check_extras
    from crawlme.config import Settings

    with patch("importlib.util.find_spec", return_value=None):
        with pytest.raises(ValueError) as error:
            _check_extras(Settings(fetcher="browser"), _extras_args(session="s.json"))
    err = str(error.value)
    assert "--session needs playwright" in err
    assert "playwright install chromium" in err, "the package alone does not fetch a browser"


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("BUDGET_PAGES", 0),
        ("FRONTIER_DRAINED", 0),
        ("MAX_RELEVANT", 0),
        ("FRONTIER_DRAINED", 0),
        ("LOGIN_REQUIRED", 1),
        ("RATE_LIMITED", 1),
        ("FATAL", 1),
        ("FRONTIER_DRAINED+DOMAIN_BUDGET", 0),
        ("BUDGET_PAGES+LOGIN_REQUIRED", 1),
        (None, 0),
        ("", 0),
    ],
)
def test_refused_exits_one(reason, code):
    """A crawl the platform refused is not a crawl that found nothing.

    A scheduled job cannot tell the two apart from a zero exit code, and
    weekly that is the difference between "no new posts" and "the
    session expired a month ago".
    """
    from crawlme.cli.report import exit_code

    assert exit_code(reason) == code


@pytest.mark.parametrize(
    ("flags", "expected_depth"),
    [
        ([], 5),
        (["--depth-limit", "1"], 1),
        (["--depth-limit", "3"], 3),
    ],
)
def test_session_sets_no_depth(_installed, tmp_path, flags, expected_depth):
    """A session file must not change the configured traversal depth."""
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
        with patch("crawlme.cli.run.create_crawl", side_effect=_capturing_factory(captured)):
            try:
                main()
            except SystemExit:
                pass
    assert captured["goal"].depth_limit == expected_depth


def test_login_refusal_says_how_to_fix_it():
    """The stop code is for a machine. This is the one ending with an
    obvious next step, and the run cannot say it any other way."""
    from crawlme.cli.report import format_summary

    out = format_summary(
        {"state": "COMPLETED", "reason": "LOGIN_REQUIRED", "session": "./ig.json", "platform": "instagram"}
    )
    assert "crawl session ./ig.json --feed instagram --force" in out


def test_advice_survives_a_missing_session_path():
    from crawlme.cli.report import format_summary

    out = format_summary({"state": "COMPLETED", "reason": "LOGIN_REQUIRED"})
    assert "crawl session" in out


def test_unjudged_pages_are_reported():
    """Fetching outruns analysis, so a run that stops on a target leaves
    pages it paid to fetch and never judged. Without this line, "not in
    the results" and "never looked at" read the same."""
    from crawlme.cli.report import format_summary

    out = format_summary(
        {"state": "COMPLETED", "reason": "MAX_RELEVANT", "pages_fetched": 78, "analyses": {"RELEVANT": 41}}
    )
    assert "37 fetched pages were never judged" in out


def test_overshooting_the_target_is_explained():
    """Work already dispatched keeps landing after the target is met.
    Without the target beside the tally, one run asking for 15 and
    reporting 24 reads as the run ignoring what it was asked for."""
    from crawlme.cli.report import format_summary

    out = format_summary(
        {"state": "COMPLETED", "reason": "MAX_RELEVANT", "max_relevant": 15, "analyses": {"RELEVANT": 24}}
    )
    assert "24 relevant against a target of 15" in out


def test_retired_sources_are_reported():
    """A run now ends by every source retiring, so without this it just
    reads as a drained frontier."""
    from crawlme.cli.report import format_summary

    out = format_summary(
        {
            "state": "COMPLETED",
            "reason": "FRONTIER_DRAINED",
            "retired_seeds": ["nothing in its last 20 pages", "nothing in its last 20 pages", "5 pages in a row older"],
        }
    )
    assert "3 sources" in out
    assert "nothing in its last 20 pages (2 sources)" in out


def test_survivors_and_rejects_are_both_listed():
    from crawlme.cli.report import format_summary

    out = format_summary(
        {
            "state": "COMPLETED",
            "reason": "FRONTIER_DRAINED",
            "seeds_asked": 2,
            "proposed_seeds": {"https://good.test/": ("worth it", (3, 8, 8, 12, 40, 45))},
            "rejected_seeds": [("https://bad.test/", "does not exist")],
        }
    )
    assert "3 relevant" in out
    assert "1 more it named that did not survive" in out
    assert "https://bad.test/  --  does not exist" in out


def test_tokens_split_by_stage():
    """A single total cannot be argued with. One run spent 500k and the
    report could not say which stage to turn down."""
    from crawlme.cli.report import format_summary

    out = format_summary(
        {
            "state": "COMPLETED",
            "reason": "FRONTIER_DRAINED",
            "llm_calls": 3,
            "tokens_by_stage": {
                "seeds": {"used": 250, "calls": 1, "in": 100, "out": 150, "cached": 0, "thinking": 140},
                "analysis": {"used": 750, "calls": 2, "in": 700, "out": 50, "cached": 400, "thinking": 0},
            },
        }
    )
    assert "analysis  750 (75%), 2 calls, 700 in / 50 out, 400 cached, 0 thinking" in out
    # Stage order, not size order: analysis leads whichever spent more.
    assert out.index("analysis") < out.index("seeds")


@pytest.mark.parametrize("command", ["run", "resume", "replay", "dedup"])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "exception", "invalid"])
async def test_dispatch_cleanup_and_report(monkeypatch, capsys, outcome, command):
    import asyncio
    import logging

    from crawlme.cli import _dispatch
    from crawlme.cli.report import make_report
    from crawlme.llm import TokenBudget
    from crawlme.runtime import Goal, Task
    from crawlme.scheduler.pipelines import Pipeline
    from crawlme.scheduler.pipelines.crawl import CrawlInput

    order = []

    class Crawl(Pipeline[CrawlInput, Task]):
        async def run(self, input, context):
            if outcome == "cancelled":
                raise asyncio.CancelledError
            if outcome == "exception":
                raise RuntimeError("unhandled")
            if outcome == "invalid":
                raise ValueError("invalid input")
            input.task.state = "FAILED" if outcome == "failed" else "COMPLETED"
            input.task.stopping_reason = "FATAL" if outcome == "failed" else "FRONTIER_DRAINED"
            return input.task

        async def aclose(self):
            order.append("pipeline closed")

        def summary(self):
            assert order == ["pipeline closed"]
            return {"pages_fetched": 3}

    async def close_clients():
        order.append("clients closed")

    monkeypatch.setattr("crawlme.cli.close_litellm_clients", close_clients)
    monkeypatch.setattr(logging.getLogger(), "level", logging.INFO)
    pipeline = Crawl()
    engine = create_engine(pipeline)
    input = CrawlInput(Goal(prompt="test"), Task(task_id="example"))

    async def cmd(args):
        await engine.run(input)
        return make_report(
            {
                **pipeline.summary(),
                "state": input.task.state,
                "reason": input.task.stopping_reason,
                "usage": TokenBudget(0).dump(),
            }
        )

    def write(text):
        assert order == ["pipeline closed", "clients closed"]

    monkeypatch.setattr(f"crawlme.cli.cmd_{command}", cmd)
    monkeypatch.setattr("crawlme.cli.write_report", write)
    call = _dispatch(argparse.Namespace(command=command))
    if outcome in {"failed", "invalid"}:
        with pytest.raises(SystemExit) as exc:
            await call
        assert exc.value.code == 1
    elif outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await call
    elif outcome == "exception":
        with pytest.raises(RuntimeError, match="unhandled"):
            await call
    else:
        await call
    assert order == ["pipeline closed", "clients closed"]
    output = capsys.readouterr().out
    assert output.count("crawl finished") == (1 if outcome in {"completed", "failed"} else 0)


@pytest.mark.parametrize("command", ["crawl", "replay", "dedup"])
def test_shared_report_sections(command):
    from crawlme.cli.report import make_report
    from crawlme.llm import TokenBudget

    budget = TokenBudget(0)
    budget.record(12, 4, cached_tokens=8, reasoning_tokens=2, stage="dedup")
    statistics = {
        "command": command,
        "state": "COMPLETED",
        "usage": budget.dump(),
        "dedup": {"status": "complete", "sources": 2, "items": 3, "groups": 1},
    }
    if command == "crawl":
        statistics.update(pages_fetched=2, fetch_errors=0, analyses={"RELEVANT": 2})
    elif command == "replay":
        statistics.update(pages_total=5, analyzed=1, retried_ok=1, published=2, skipped=2, empty=1)
    report = make_report(statistics)
    assert report.code == 0
    assert "tokens:     16 (12 in / 4 out), 1 calls" in report.text
    assert "3 items from 2 pages -> 1 results" in report.text
    assert "8 (67% of input)" in report.text
    assert "2 (50% of output)" in report.text
    assert "by stage:" in report.text
    if command == "replay":
        assert "2 written (1 first-try, 1 retried)" in report.text
        assert "2 identical, 1 empty text" in report.text
    if command != "crawl":
        assert "fetched" not in report.text
        assert "fetch failures" not in report.text
    assert "tokens_used" not in statistics  # Formatting does not mutate its input.


@pytest.fixture(autouse=True)
def no_cli_monitor(monkeypatch):
    monkeypatch.setattr("crawlme.cli.run.create_monitor", lambda pipeline: None)
