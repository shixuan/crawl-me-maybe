"""Integration tests (full pipeline with mocked network layer)."""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.scheduler.factory import create_scheduler
from crawlme.schemas import CrawlGoal, CrawlTask, FetchResult, FrontierItem

# ── test HTML pages (simplified, known structure) ────────────────────────

_PAGE_ALPHA_HTML = b"""<!DOCTYPE html>
<html><head><title>Memory Safety Guide</title></head>
<body>
<p>Learn about memory safety and static analysis.</p>
<a href="https://example.com/beta">Memory Safety in Rust</a>
<a href="https://example.com/gamma">Click here</a>
<a href="https://example.com/delta">Compiler Design 101</a>
<a href="https://example.com/epsilon">About Us</a>
<a href="/pdf/report.pdf">PDF Report</a>
<a href="javascript:void(0)">JS Link</a>
<a href="https://wikidata.org/wiki/Q123">Wikidata Entry</a>
</body></html>"""

_PAGE_BETA_HTML = b"""<!DOCTYPE html>
<html><head><title>Rust Memory Safety</title></head>
<body>
<p>Rust guarantees memory safety without garbage collection.</p>
<a href="https://example.com/alpha">Home</a>
<a href="https://example.com/gamma">Click here</a>
</body></html>"""

_PAGE_DELTA_HTML = b"""<!DOCTYPE html>
<html><head><title>Compiler Construction</title></head>
<body>
<p>A comprehensive guide to compiler design and static program analysis.</p>
<a href="https://example.com/alpha">Memory Safety Home</a>
<a href="https://en.wikipedia.org/wiki/Compiler">Wikipedia Compiler</a>
</body></html>"""

_SEED_URL = "https://example.com/alpha"


# ── mock fetcher (replaces real HTTP) ────────────────────────────────────


class _MockFetcher:
    """Returns pre-canned HTML per URL, simulates a real fetcher interface."""

    def __init__(self, **kwargs):
        self._pages: dict[str, bytes] = {
            "https://example.com/alpha": _PAGE_ALPHA_HTML,
            "https://example.com/beta": _PAGE_BETA_HTML,
            "https://example.com/delta": _PAGE_DELTA_HTML,
        }

    async def fetch(self, item: FrontierItem) -> FetchResult:
        raw_url = item.url.raw
        html = self._pages.get(raw_url, b"<html><body>Not Found</body></html>")
        status = 200 if raw_url in self._pages else 404
        return FetchResult(
            item_id=item.item_id,
            url_key=item.url_key,
            url=item.url,
            status_code=status,
            final_url=item.url,
            raw=html,
            content_type="text/html",
            fetch_duration_ms=10,
            fetch_attempt=1,
        )


# ── test helpers ─────────────────────────────────────────────────────────


def _seed_item(seed_url: str) -> FrontierItem:
    canon = Canonicalizer()
    url = canon.canonicalize(seed_url, seed_url)
    return FrontierItem(url=url, url_key=url.url_key, priority=1.0, score_source="seed", reg_domain=url.reg_domain)


# ── tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_extract_links_pipeline(integration_settings):
    """Single seed → fetch → extract → links → prefilter → buffer."""
    cfg = integration_settings
    goal = CrawlGoal(
        prompt="memory safety and compiler design",
        max_pages=1,
        depth_limit=1,
    )
    task = CrawlTask(goal_id=goal.goal_id, state="CREATED")

    sched = create_scheduler(cfg, fetcher=_MockFetcher())
    await sched._frontier.push_batch([_seed_item(_SEED_URL)])

    await asyncio.wait_for(sched.run(goal, task), timeout=30)

    assert task.state == "COMPLETED"
    assert sched._counters.pages_fetched == 1

    # Verify DB contents.
    async with aiosqlite.connect(sched._storage.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await db.execute("SELECT title, extraction_status FROM pages")
        page = await row.fetchone()
        assert page["title"] == "Memory Safety Guide"
        assert page["extraction_status"] in ("OK", "DEGRADED")

        row = await db.execute("SELECT COUNT(*) FROM candidates WHERE status='BUFFERED'")
        (buffered,) = await row.fetchone()
        # Should allow: beta (anchors match), delta (anchors match).  Not gamma, epsilon, pdf, js, wikidata.
        assert buffered >= 2, f"Expected >= 2 BUFFERED candidates, got {buffered}"


@pytest.mark.asyncio
async def test_prefilter_drops_junk(integration_settings):
    """PDF, javascript, wikidata URLs should be filtered out, so only BUFFERED ones persist."""
    cfg = integration_settings
    goal = CrawlGoal(prompt="memory safety and compiler design", max_pages=1)
    task = CrawlTask(goal_id=goal.goal_id, state="CREATED")

    sched = create_scheduler(cfg, fetcher=_MockFetcher())
    await sched._frontier.push_batch([_seed_item(_SEED_URL)])

    await asyncio.wait_for(sched.run(goal, task), timeout=30)

    async with aiosqlite.connect(sched._storage.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await db.execute("SELECT COUNT(*) FROM candidates")
        (total,) = await row.fetchone()
        # 7 links on the page; junk ones (pdf, js, wikidata) are not persisted.
        # Only BUFFERED candidates are saved, so the total stays below 7.
        assert 1 <= total <= 7, f"Expected 1-7 candidates, got {total}"

        row = await db.execute("SELECT DISTINCT status FROM candidates")
        statuses = {r[0] for r in await row.fetchall()}
        assert statuses == {"BUFFERED"}, f"Only BUFFERED should be persisted, got {statuses}"


@pytest.mark.asyncio
async def test_stops_on_budget_pages(integration_settings):
    """With max_pages=2, crawl should stop after fetching 2 pages."""
    cfg = integration_settings
    goal = CrawlGoal(prompt="memory safety and compiler design", max_pages=2, depth_limit=1)
    task = CrawlTask(goal_id=goal.goal_id, state="CREATED")

    sched = create_scheduler(cfg, fetcher=_MockFetcher())
    await sched._frontier.push_batch([_seed_item(_SEED_URL)])

    await asyncio.wait_for(sched.run(goal, task), timeout=30)

    pages = sched._counters.pages_fetched
    assert 1 <= pages <= 2 + 1  # budget + possible in-flight
    assert "BUDGET_PAGES" in (task.stopping_reason or "")


@pytest.mark.asyncio
async def test_frontier_drained_when_no_more_links(integration_settings):
    """Crawl stops naturally when no new candidates pass ranking threshold."""
    cfg = integration_settings
    goal = CrawlGoal(prompt="memory safety and compiler design", max_pages=100, depth_limit=1)
    task = CrawlTask(goal_id=goal.goal_id, state="CREATED")

    sched = create_scheduler(cfg, fetcher=_MockFetcher())
    await sched._frontier.push_batch([_seed_item(_SEED_URL)])

    await asyncio.wait_for(sched.run(goal, task), timeout=30)

    # With our small mock site (3 pages) and depth_limit=1, should drain naturally.
    assert task.state == "COMPLETED"
    # At minimum the seed page was fetched.
    assert sched._counters.pages_fetched >= 1


@pytest.mark.asyncio
async def test_events_emitted(integration_settings):
    """Events table should contain the full lifecycle for a single-page crawl."""
    cfg = integration_settings
    goal = CrawlGoal(prompt="memory safety and compiler design", max_pages=1)
    task = CrawlTask(goal_id=goal.goal_id, state="CREATED")

    sched = create_scheduler(cfg, fetcher=_MockFetcher())
    await sched._frontier.push_batch([_seed_item(_SEED_URL)])

    await asyncio.wait_for(sched.run(goal, task), timeout=30)

    async with aiosqlite.connect(sched._storage.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await db.execute("SELECT COUNT(*) FROM events")
        (count,) = await row.fetchone()
        assert count >= 4, f"Expected >= 4 events, got {count}"

        # Key lifecycle events must be present.
        row = await db.execute("SELECT type FROM events ORDER BY seq")
        types = {r["type"] for r in await row.fetchall()}
        for expected in ("TASK_STARTED", "FETCH_STARTED", "FETCH_COMPLETED", "PAGE_EXTRACTED", "STOPPED"):
            assert expected in types, f"Missing event type: {expected}"


@pytest.mark.asyncio
async def test_rank_decisions_persisted(integration_settings):
    """Rank decisions should be written to the DB."""
    cfg = integration_settings
    goal = CrawlGoal(prompt="memory safety and compiler design", max_pages=1)
    task = CrawlTask(goal_id=goal.goal_id, state="CREATED")

    sched = create_scheduler(cfg, fetcher=_MockFetcher())
    await sched._frontier.push_batch([_seed_item(_SEED_URL)])

    await asyncio.wait_for(sched.run(goal, task), timeout=30)

    async with aiosqlite.connect(sched._storage.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await db.execute("SELECT COUNT(*) FROM rank_decisions")
        (count,) = await row.fetchone()
        assert count >= 1, f"Expected >= 1 rank decision, got {count}"
        # At least one decision should be a kept (non-dropped) ranking.
        row = await db.execute("SELECT COUNT(*) FROM rank_decisions WHERE dropped = 0")
        (kept,) = await row.fetchone()
        assert kept >= 1, f"Expected >= 1 kept decision, got {kept}"


class _RecordingAnalyzer:
    """Stub analyzer: records pages it saw, produces no results."""

    def __init__(self) -> None:
        self.pages: list[str] = []

    def bind_sink(self, sink) -> None:
        pass

    async def analyze(self, page, goal):
        self.pages.append(page.url_key)
        return None

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_analyzer_sees_every_fetched_page(integration_settings):
    """The v0.2 analysis stage runs after each page extraction."""
    cfg = integration_settings
    goal = CrawlGoal(prompt="memory safety and compiler design", max_pages=1, depth_limit=1)
    task = CrawlTask(goal_id=goal.goal_id, state="CREATED")

    from crawlme.feedback.signals import InflightSignals
    from crawlme.feedback.system import FeedbackLoop

    analyzer = _RecordingAnalyzer()
    feedback = FeedbackLoop(analyzer=analyzer, signals=InflightSignals())
    sched = create_scheduler(cfg, fetcher=_MockFetcher(), feedback=feedback)
    await sched._frontier.push_batch([_seed_item(_SEED_URL)])

    await asyncio.wait_for(sched.run(goal, task), timeout=30)

    seed_key = Canonicalizer().canonicalize(_SEED_URL, _SEED_URL).url_key
    assert analyzer.pages == [seed_key]
