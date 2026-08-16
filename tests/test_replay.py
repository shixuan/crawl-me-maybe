"""Tests for replay: re-running the analysis stage over a finished run.

The run database is faked with real SqliteCrawlDb writes; the analyzer
is stubbed through the Analyzer protocol so no LLM is ever called.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crawlme.cli.replay import ReplayError, find_run_dir, run_replay
from crawlme.config import Settings
from crawlme.llm import TokenBudgetError
from crawlme.schemas import URL, AnalysisResult, CrawlGoal, CrawlTask, Page
from crawlme.storage.sqlite.crawl_db import SqliteCrawlDb


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
    db = SqliteCrawlDb(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
    await db.start()
    db.save_goal(goal.model_dump(mode="json"))
    db.save_task(CrawlTask(task_id=task_id, goal_id=goal.goal_id).model_dump(mode="json"))
    for page in pages or []:
        db.save_page(page)
    await db.close()
    return run_dir


class _StubAnalyzer:
    """Analyzer-protocol fake: scripted results, call recording.

    Mirrors the real contract: every success is published through the
    bound sink.  park=True simulates transient failures instead (the
    page returns None and settles on the drain).
    """

    def __init__(
        self,
        *,
        park: bool = False,
        raise_error: Exception | None = None,
        publish_on_drain: list[AnalysisResult] | None = None,
    ) -> None:
        self.sink = None
        self.calls: list[tuple[str, str]] = []
        self.drained = False
        self.closed = False
        self._park = park
        self._raise = raise_error
        self._drain_publish = list(publish_on_drain or [])

    def bind_sink(self, sink) -> None:
        self.sink = sink

    async def analyze(self, page, goal):
        if self._raise is not None:
            self.calls.append((page.url_key, goal.goal_id))
            raise self._raise
        self.calls.append((page.url_key, goal.goal_id))
        if self._park:
            return None
        result = AnalysisResult(
            page_id=page.page_id,
            url_key=page.url_key,
            goal_id=goal.goal_id,
            classification="RELEVANT",
            relevance_score=0.9,
            summary="fine",
            model="stub-model",
            prompt_version="v2.4",
            tokens_used=100,
        )
        if self.sink is not None:
            self.sink(result)
        return result

    async def drain_pending(self) -> None:
        self.drained = True
        for result in self._drain_publish:
            self.sink(result)

    async def aclose(self) -> None:
        self.closed = True


def _cfg(tmp_path: Path) -> Settings:
    # Credentials and model pinned so no developer .env leaks a real
    # LLM call or changes the identity check's model column.
    return Settings(llm_api_key="", llm_base_url="", llm_model="", result_dir=tmp_path)


async def _read_analyses(run_dir: Path, url_key: str) -> list[dict]:
    db = SqliteCrawlDb(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
    await db.start()
    try:
        return await db.get_analyses_by_url_key(url_key)
    finally:
        await db.close()


async def _read_goal(run_dir: Path, goal_id: str) -> dict | None:
    db = SqliteCrawlDb(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
    await db.start()
    try:
        return await db.get_goal(goal_id)
    finally:
        await db.close()


# -- find_run_dir --------------------------------------------------------


@pytest.mark.asyncio
async def test_find_run_dir_picks_the_run_holding_the_task(tmp_path):
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
async def test_find_run_dir_lists_what_was_scanned(tmp_path):
    await _write_run(tmp_path, "20260101_000001", task_id="known")
    with pytest.raises(ReplayError, match="known"):
        await find_run_dir(tmp_path, "missing")


@pytest.mark.asyncio
async def test_find_run_dir_with_no_runs_at_all(tmp_path):
    with pytest.raises(ReplayError, match="no run databases"):
        await find_run_dir(tmp_path, "missing")


# -- the replay run ------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_analyzes_and_persists(tmp_path):
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
    assert analyzer.closed and analyzer.drained
    # Every analysis landed in the run's db under the original goal.
    rows = await _read_analyses(run_dir, "a")
    assert len(rows) == 1
    assert rows[0]["goal_id"] == report.goal_id
    assert (await _read_goal(run_dir, report.goal_id))["prompt"] == "find rust posts"


@pytest.mark.asyncio
async def test_replay_of_replay_is_a_noop(tmp_path):
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
async def test_replay_force_reruns_and_keeps_old_rows(tmp_path):
    run_dir = await _write_run(tmp_path, "20260101_000001", pages=[_page("a")])
    cfg = _cfg(tmp_path)
    await run_replay(cfg, "task1", analyzer=_StubAnalyzer())

    forced = await run_replay(cfg, "task1", analyzer=_StubAnalyzer(), force=True)

    assert forced.skipped == 0
    assert forced.published == 1
    # Append-only: the old row stays, the forced run adds its own.
    assert len(await _read_analyses(run_dir, "a")) == 2


@pytest.mark.asyncio
async def test_replay_with_prompt_creates_new_goal(tmp_path):
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
async def test_replay_same_prompt_reuses_the_goal_and_skips(tmp_path):
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
async def test_replay_limit_caps_the_work(tmp_path):
    await _write_run(tmp_path, "20260101_000001", pages=[_page("a"), _page("b"), _page("c")])
    analyzer = _StubAnalyzer()

    report = await run_replay(_cfg(tmp_path), "task1", analyzer=analyzer, limit=1)

    assert report.pages_total == 3
    assert report.analyzed == 1
    assert analyzer.calls == [("a", report.goal_id)]


@pytest.mark.asyncio
async def test_replay_skips_empty_pages(tmp_path):
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
async def test_replay_counts_retried_and_failed(tmp_path):
    await _write_run(tmp_path, "20260101_000001", pages=[_page("a"), _page("b")])
    # Both pages park; one settles on the drain, the other gives up.
    analyzer = _StubAnalyzer(park=True, publish_on_drain=[AnalysisResult(page_id="p-a", url_key="a")])

    report = await run_replay(_cfg(tmp_path), "task1", analyzer=analyzer)

    assert report.analyzed == 0
    assert report.retried_ok == 1
    assert report.published == 1
    assert report.failed == 1


@pytest.mark.asyncio
async def test_replay_stops_when_the_budget_breaks(tmp_path):
    await _write_run(tmp_path, "20260101_000001", pages=[_page("a"), _page("b")])
    analyzer = _StubAnalyzer(raise_error=TokenBudgetError("budget exhausted"))

    report = await run_replay(_cfg(tmp_path), "task1", analyzer=analyzer)

    assert report.analyzed == 0
    assert analyzer.calls == [("a", report.goal_id)]  # the first call broke the loop


@pytest.mark.asyncio
async def test_replay_without_credentials_raises(tmp_path):
    await _write_run(tmp_path, "20260101_000001", pages=[_page("a")])
    with pytest.raises(ReplayError, match="credentials"):
        await run_replay(_cfg(tmp_path), "task1")
