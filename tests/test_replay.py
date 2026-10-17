"""Tests for replay: re-running the analysis stage over a finished run.

The run database is faked with real Storage writes; the analyzer
is stubbed through the Analyzer protocol so no LLM is ever called.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from crawlme.analysis import AnalysisError, AnalysisResult
from crawlme.cli.replay import run_replay
from crawlme.config import Settings
from crawlme.digest import Page
from crawlme.errors import Error, ErrorHandler, ErrorPolicy, Kind
from crawlme.llm import RemoteError, TokenBudgetError
from crawlme.persist import Storage
from crawlme.persist.read import find_run_dir
from crawlme.runtime import CrawlGoal, CrawlTask
from crawlme.scheduler.pipelines.replay import ReplayError
from crawlme.urls import URL


def _goal(prompt: str = "find rust posts") -> CrawlGoal:
    return CrawlGoal(prompt=prompt)


def _page(key: str, *, text: str = "hello world") -> Page:
    return Page(
        page_id=f"p-{key}",
        url_key=key,
        url=URL(
            raw=f"https://example.com/{key}",
            canonical=f"https://example.com/{key}",
            url_key=key,
            reg_domain="example.com",
        ),
        title=f"Title {key}",
        plain_text=text,
    )


async def _write_run(
    root: Path,
    ts: str,
    *,
    task_id: str = "task1",
    goal: CrawlGoal | None = None,
    pages: list[Page] | None = None,
) -> Path:
    """Create a fake run dir root/<ts>/ holding a goal, a task, pages."""
    goal = goal or _goal()
    run_dir = root / ts
    (run_dir / "db").mkdir(parents=True)
    db = Storage(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
    await db.start()
    db.save_goal(goal.model_dump(mode="json"))
    db.save_task(CrawlTask(task_id=task_id, goal_id=goal.goal_id).model_dump(mode="json"))
    for page in pages or []:
        db.save_page(page)
    await db.close()
    return run_dir


class _StubAnalyzer:
    """Script single analysis attempts; the real worker owns retries and publishing."""

    def __init__(self, *, park=False, raise_error=None, publish_on_drain=None, park_error=None):
        self.calls = []
        self._park = park
        self._park_error = park_error or RemoteError("temporary", kind=Kind.UNAVAILABLE)
        self._raise = raise_error
        self._results = {result.url_key: result for result in publish_on_drain or []}

    async def analyze(self, page, goal):
        previous = sum(key == page.url_key for key, _ in self.calls)
        self.calls.append((page.url_key, goal.goal_id))
        if self._raise is not None:
            raise self._raise
        if self._park:
            if previous and page.url_key in self._results:
                return self._results[page.url_key]
            raise self._park_error
        return AnalysisResult(
            page_id=page.page_id,
            url_key=page.url_key,
            goal_id=goal.goal_id,
            classification="RELEVANT",
            relevance_score=0.9,
            summary="fine",
            model="stub-model",
            tokens_used=100,
        )


@pytest.fixture(autouse=True)
def immediate_retries(monkeypatch):
    monkeypatch.setattr(
        "crawlme.scheduler.pipelines.factory.ErrorHandler", lambda: ErrorHandler(ErrorPolicy(retry_delay=0))
    )


def _cfg(tmp_path: Path) -> Settings:
    # Credentials and model pinned so no developer .env leaks a real
    # LLM call or changes the identity check's model column.
    return Settings(llm_api_key="", llm_base_url="", llm_model="", result_dir=tmp_path)


async def _read_analyses(run_dir: Path, url_key: str) -> list[dict]:
    db = Storage(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
    await db.start()
    try:
        return await db.get_analyses_by_url_key(url_key)
    finally:
        await db.close()


async def _read_goal(run_dir: Path, goal_id: str) -> dict | None:
    db = Storage(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
    await db.start()
    try:
        return await db.get_goal(goal_id)
    finally:
        await db.close()


# -- find_run_dir --------------------------------------------------------


@pytest.mark.asyncio
async def test_find_run_picks(tmp_path):
    await _write_run(tmp_path, "20260101_000001", task_id="other", pages=[_page("x")])
    target = await _write_run(tmp_path, "20260101_000002", task_id="wanted", pages=[_page("a")])
    await _write_run(tmp_path, "20260101_000003", task_id="third")
    # Stray files and deeper benchmark dirs must not confuse the scan.
    (tmp_path / "feedback.db").write_text("")
    bench = tmp_path / "bench" / "goalhash" / "20260101_000004" / "arm" / "db"
    bench.mkdir(parents=True)
    (bench / "crawl.db").write_text("not a database")

    run_dir, row = await find_run_dir(tmp_path, "wanted")
    assert run_dir == target
    assert row["task_id"] == "wanted"


@pytest.mark.asyncio
async def test_find_run_lists(tmp_path):
    await _write_run(tmp_path, "20260101_000001", task_id="known")
    with pytest.raises(Error, match="known"):
        await find_run_dir(tmp_path, "missing")


@pytest.mark.asyncio
async def test_find_run_none(tmp_path):
    with pytest.raises(Error, match="no run databases"):
        await find_run_dir(tmp_path, "missing")


# -- the replay run ------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_persists(tmp_path):
    run_dir = await _write_run(tmp_path, "20260101_000001", pages=[_page("a"), _page("b")])
    analyzer = _StubAnalyzer()

    report = await run_replay(_cfg(tmp_path), "task1", analyzer=analyzer)

    assert report.run_dir == run_dir
    assert report.pages_total == 2
    assert report.analyzed == 2
    assert report.published == 2
    assert report.skipped == 0
    assert report.failed == 0
    assert report.new_goal is False
    # Every analysis landed in the run's db under the original goal.
    rows = await _read_analyses(run_dir, "a")
    assert len(rows) == 1
    assert rows[0]["goal_id"] == report.goal_id
    assert (await _read_goal(run_dir, report.goal_id))["prompt"] == "find rust posts"


@pytest.mark.asyncio
async def test_replay_twice(tmp_path):
    await _write_run(tmp_path, "20260101_000001", pages=[_page("a"), _page("b")])
    cfg = _cfg(tmp_path)
    first = await run_replay(cfg, "task1", analyzer=_StubAnalyzer())
    assert first.published == 2

    second_analyzer = _StubAnalyzer()
    second = await run_replay(cfg, "task1", analyzer=second_analyzer)

    assert second.published == 0
    assert second.skipped == 2
    assert second_analyzer.calls == []  # no LLM calls at all


@pytest.mark.asyncio
async def test_replay_force(tmp_path):
    run_dir = await _write_run(tmp_path, "20260101_000001", pages=[_page("a")])
    cfg = _cfg(tmp_path)
    await run_replay(cfg, "task1", analyzer=_StubAnalyzer())

    forced = await run_replay(cfg, "task1", analyzer=_StubAnalyzer(), force=True)

    assert forced.skipped == 0
    assert forced.published == 1
    # Append-only: the old row stays, the forced run adds its own.
    assert len(await _read_analyses(run_dir, "a")) == 2


@pytest.mark.asyncio
async def test_replay_newgoal(tmp_path):
    original = _goal()
    run_dir = await _write_run(tmp_path, "20260101_000001", goal=original, pages=[_page("a")])
    analyzer = _StubAnalyzer()

    report = await run_replay(_cfg(tmp_path), "task1", analyzer=analyzer, prompt="a brand new goal")

    assert report.new_goal
    assert report.goal_id != original.goal_id
    assert analyzer.calls == [("a", report.goal_id)]
    # Both goals live in the run db; the new one carries the raw prompt
    # (the enhancer is inert without credentials).
    assert (await _read_goal(run_dir, report.goal_id))["prompt"] == "a brand new goal"
    assert (await _read_goal(run_dir, original.goal_id))["prompt"] == "find rust posts"
    # Analyses go under the new goal_id, never the original one.
    rows = await _read_analyses(run_dir, "a")
    assert [r["goal_id"] for r in rows] == [report.goal_id]


@pytest.mark.asyncio
async def test_replay_samegoal(tmp_path):
    await _write_run(tmp_path, "20260101_000001", pages=[_page("a"), _page("b")])
    cfg = _cfg(tmp_path)
    first = await run_replay(cfg, "task1", analyzer=_StubAnalyzer(), prompt="new lens")
    assert first.new_goal
    assert first.published == 2

    second = await run_replay(cfg, "task1", analyzer=_StubAnalyzer(), prompt="new lens")

    # Same prompt text, same judging context: no new goal row, no calls.
    assert second.new_goal is False
    assert second.goal_id == first.goal_id
    assert second.published == 0
    assert second.skipped == 2

    # A different prompt is a different context: the shared pages are
    # judged again, under the new goal id.
    third = await run_replay(cfg, "task1", analyzer=_StubAnalyzer(), prompt="another lens")
    assert third.goal_id != first.goal_id
    assert third.published == 2


@pytest.mark.asyncio
async def test_replay_dup_id(tmp_path):
    prompt = "new lens"
    original = _goal("original prompt")
    run_dir = tmp_path / "20260101_000001"
    (run_dir / "db").mkdir(parents=True)
    db = Storage(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
    await db.start()
    db.save_goal(original.model_dump(mode="json"))
    # A row already occupies the new prompt's derived id, with different
    # text (hand-edited data or a hash collision): refuse to touch it.
    db.save_goal(
        {
            "goal_id": CrawlGoal(prompt=prompt).goal_id,
            "prompt": "different text",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    db.save_task(CrawlTask(task_id="task1", goal_id=original.goal_id).model_dump(mode="json"))
    await db.close()

    with pytest.raises(ReplayError, match="different prompt"):
        await run_replay(_cfg(tmp_path), "task1", analyzer=_StubAnalyzer(), prompt=prompt)


@pytest.mark.asyncio
async def test_replay_limit(tmp_path):
    await _write_run(tmp_path, "20260101_000001", pages=[_page("a"), _page("b"), _page("c")])
    analyzer = _StubAnalyzer()

    report = await run_replay(_cfg(tmp_path), "task1", analyzer=analyzer, limit=1)

    assert report.pages_total == 3
    assert report.analyzed == 1
    assert analyzer.calls == [("a", report.goal_id)]


@pytest.mark.asyncio
async def test_replay_empty(tmp_path):
    await _write_run(
        tmp_path,
        "20260101_000001",
        pages=[_page("a"), _page("b", text=""), _page("c")],
    )
    analyzer = _StubAnalyzer()

    report = await run_replay(_cfg(tmp_path), "task1", analyzer=analyzer)

    assert report.analyzed == 2
    assert report.empty == 1
    assert [call[0] for call in analyzer.calls] == ["a", "c"]


@pytest.mark.asyncio
async def test_replay_counts(tmp_path):
    await _write_run(tmp_path, "20260101_000001", pages=[_page("a"), _page("b")])
    # Both pages park; one settles on the drain, the other gives up.
    analyzer = _StubAnalyzer(
        park=True,
        park_error=AnalysisError("invalid output"),
        publish_on_drain=[AnalysisResult(page_id="p-a", url_key="a")],
    )

    report = await run_replay(_cfg(tmp_path), "task1", analyzer=analyzer)

    assert report.analyzed == 0
    assert report.retried_ok == 1
    assert report.published == 1
    assert report.failed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 2, 3])
async def test_replay_concurrency(tmp_path, concurrency):
    await _write_run(tmp_path, "20260101_000001", pages=[_page(str(i)) for i in range(7)])

    class Analyzer(_StubAnalyzer):
        active = 0
        peak = 0

        async def analyze(self, page, goal):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.01)
                return await super().analyze(page, goal)
            finally:
                self.active -= 1

    analyzer = Analyzer()
    cfg = _cfg(tmp_path)
    cfg.llm_concurrency = concurrency
    report = await run_replay(cfg, "task1", analyzer=analyzer)
    assert analyzer.peak == concurrency
    assert analyzer.active == 0
    assert len({key for key, _ in analyzer.calls}) == 7
    assert report.analyzed == report.published == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_error", [True, False])
async def test_replay_inflight_cleanup(tmp_path, budget_error):
    await _write_run(tmp_path, "20260101_000001", pages=[_page(str(i)) for i in range(6)])
    started = asyncio.Event()
    failed = asyncio.Event()

    class Analyzer(_StubAnalyzer):
        entered = 0
        active = 0

        async def analyze(self, page, goal):
            self.entered += 1
            self.active += 1
            try:
                if self.entered == 1:
                    await started.wait()
                    failed.set()
                    if budget_error:
                        raise TokenBudgetError("exhausted")
                    raise RuntimeError("broken")
                started.set()
                await failed.wait()
                if not budget_error:
                    await asyncio.Event().wait()
                return await super().analyze(page, goal)
            finally:
                self.active -= 1

    analyzer = Analyzer()
    cfg = _cfg(tmp_path)
    cfg.llm_concurrency = 2
    if budget_error:
        report = await asyncio.wait_for(run_replay(cfg, "task1", analyzer=analyzer), timeout=3)
        assert report.analyzed == report.published == 1
        assert report.failed == 0
    else:
        with pytest.raises(Error, match="broken"):
            await asyncio.wait_for(run_replay(cfg, "task1", analyzer=analyzer), timeout=3)
    assert analyzer.entered == 2
    assert analyzer.active == 0


@pytest.mark.asyncio
async def test_replay_budget(tmp_path):
    await _write_run(tmp_path, "20260101_000001", pages=[_page("a"), _page("b")])
    analyzer = _StubAnalyzer(raise_error=TokenBudgetError("budget exhausted"))

    report = await run_replay(_cfg(tmp_path), "task1", analyzer=analyzer)

    assert report.analyzed == 0
    assert analyzer.calls == [("a", report.goal_id)]  # the first call broke the loop


@pytest.mark.asyncio
async def test_replay_no_creds(tmp_path):
    await _write_run(tmp_path, "20260101_000001", pages=[_page("a")])
    with pytest.raises(ReplayError, match="credentials"):
        await run_replay(_cfg(tmp_path), "task1")


async def test_replay_outage_stops(tmp_path):
    run_dir = await _write_run(tmp_path, "20260101_000001", pages=[_page("a"), _page("b")])
    analyzer = _StubAnalyzer(park=True, publish_on_drain=[AnalysisResult(page_id="p-a", url_key="a")])
    with pytest.raises(RemoteError, match="temporary"):
        await run_replay(_cfg(tmp_path), "task1", analyzer=analyzer)
    assert sum(key == "b" for key, _ in analyzer.calls) == 3
    assert len(await _read_analyses(run_dir, "a")) == 1


@pytest.mark.parametrize("retry", [False, True])
async def test_replay_cancel_closes_work(tmp_path, monkeypatch, retry):
    """Cancellation stops requests and retries before the pipeline closes storage."""
    run_dir = await _write_run(tmp_path, "20260101_000001", pages=[_page("a"), _page("b")])
    entered = asyncio.Event()
    active = 0
    attempts = {}

    class WaitingAnalyzer:
        async def analyze(self, page, goal):
            nonlocal active
            attempts[page.url_key] = attempts.get(page.url_key, 0) + 1
            if retry and attempts[page.url_key] == 1:
                raise RemoteError("temporary", kind=Kind.UNAVAILABLE)
            active += 1
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1

    close = Storage.close
    closed = []

    async def checked_close(storage):
        assert active == 0
        await close(storage)
        closed.append(storage.db_path)

    monkeypatch.setattr(Storage, "close", checked_close)
    cfg = _cfg(tmp_path)
    cfg.llm_concurrency = 2
    running = asyncio.create_task(run_replay(cfg, "task1", analyzer=WaitingAnalyzer()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 2)
        assert active == 0 and len(closed) == 1
        assert await _read_analyses(run_dir, "a") == []
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("enabled", [True, False])
async def test_replay_groups_after_retries(tmp_path, monkeypatch, enabled):
    """Grouping sees committed retry results for the replay goal and shares its budget."""
    import sqlite3

    from crawlme.dedup import Group

    run_dir = await _write_run(tmp_path, "20260101_000001", pages=[_page("a"), _page("b")])
    goal = _goal()
    results = [
        AnalysisResult(
            page_id=f"p-{key}", url_key=key, goal_id=goal.goal_id, classification="RELEVANT", summary="same item"
        )
        for key in ("a", "b")
    ]
    analyzer = _StubAnalyzer(park=True, publish_on_drain=results)
    grouped = []

    def build(settings, *, budget):
        assert enabled

        class Grouper:
            async def group(self, selected, rows):
                assert selected.goal_id == goal.goal_id
                assert {r["url"] for r in rows} == {"https://example.com/a", "https://example.com/b"}
                assert all(sum(key == r.url_key for key, _ in analyzer.calls) == 2 for r in results)
                grouped.append(True)
                budget.record(12, 4, cached_tokens=8, stage="dedup")
                return [Group(members=[r["item_id"] for r in rows], overview="same item")]

        return Grouper()

    monkeypatch.setattr("crawlme.scheduler.pipelines.factory.Grouper.from_settings", build)
    result = await run_replay(_cfg(tmp_path), "task1", analyzer=analyzer, dedup_enabled=enabled)
    assert result.retried_ok == result.published == 2
    assert grouped == ([True] if enabled else [])
    assert result.dedup["status"] == ("complete" if enabled else "disabled")
    assert result.usage["used"] == (16 if enabled else 0)
    with sqlite3.connect(run_dir / "db" / "crawl.db") as db:
        assert db.execute("SELECT count(*) FROM dedup_runs").fetchone()[0] == int(enabled)
        assert db.execute("SELECT count(*) FROM analyses").fetchone()[0] == 2
