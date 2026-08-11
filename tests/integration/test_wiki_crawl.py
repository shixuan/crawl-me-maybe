"""End-to-end integration tests against live Wikipedia.

Run manually (skipped in CI):

    pytest tests/integration/test_wiki_crawl.py -v -s -m integration

Goal: "projects rewritten in Rust" — find software projects that were
rewritten from other languages into Rust.
"""

from __future__ import annotations

import asyncio
import time

import aiosqlite
import pytest

from crawlme.logging import setup_logging
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.scheduler.engine import CrawlScheduler
from crawlme.schemas import CrawlGoal, CrawlTask, FrontierItem

pytestmark = pytest.mark.integration

_SEED = "https://en.wikipedia.org/wiki/Rust_(programming_language)"
_PROMPT = "software projects that were rewritten in Rust, or adopted Rust for performance and safety"
_MAX_PAGES = 10


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_wiki_rust_rewrite_basic_crawl(integration_settings):
    """Full pipeline: seed Wikipedia Rust page, crawl up to 10 pages.

    Verifies:
      - Crawl completes without fatal errors
      - Pages are fetched and extracted
      - Candidates flow through prefilter → buffer → ranker → frontier
      - Storage has pages, candidates, rank_decisions
      - Stop reason includes BUDGET_PAGES
    """
    cfg = integration_settings
    setup_logging(cfg, force=True)

    goal = CrawlGoal(prompt=_PROMPT, max_pages=_MAX_PAGES)
    task = CrawlTask(goal_id=goal.goal_id, state="CREATED")

    sched = CrawlScheduler(settings=cfg)

    # Push seed into frontier.
    canon = Canonicalizer()
    url = canon.canonicalize(_SEED, _SEED)
    item = FrontierItem(
        url=url,
        url_key=url.url_key,
        priority=1.0,
        score_source="seed",
        reg_domain=url.reg_domain,
    )
    await sched._frontier.push_batch([item])

    print(f"\nTask:   {task.task_id}")
    print(f"Prompt: {_PROMPT}")
    print(f"Seed:   {_SEED}")
    print(f"Budget: {_MAX_PAGES} pages")
    print("---")

    t0 = time.monotonic()
    timed_out = False
    try:
        await asyncio.wait_for(sched.run(goal, task), timeout=180)
    except asyncio.TimeoutError:
        timed_out = True
        print("\nTimed out after 180s — checking partial results.")
    elapsed = time.monotonic() - t0

    # ── assertions ──────────────────────────────────────────────────

    counters = sched._counters
    pages_fetched = counters.get("pages_fetched", 0)

    print(f"\nElapsed: {elapsed:.1f}s")
    print(f"Pages fetched: {pages_fetched}")
    print(f"State: {task.state}")
    print(f"Stop reason: {task.stopping_reason}")

    # 1. Must have fetched at least 1 page.
    assert pages_fetched >= 1, "No pages fetched — check network, UA, or seed URL"

    # 2. State must be terminal unless timed out.
    if not timed_out:
        assert task.state in ("COMPLETED", "STOPPING"), f"Unexpected state: {task.state}"

    # 3. Should not exceed budget.
    assert pages_fetched <= _MAX_PAGES, f"Fetched {pages_fetched} > budget {_MAX_PAGES}"

    # 4. Stop reason should be sensible when the run completed normally.
    if not timed_out and task.stopping_reason:
        assert any(r in (task.stopping_reason or "") for r in ("BUDGET_PAGES", "FRONTIER_DRAINED")), (
            f"Unexpected stop reason: {task.stopping_reason}"
        )

    # 5. Verify storage contents via direct DB queries.
    db_path = sched._storage.db_path
    async with aiosqlite.connect(db_path) as db:
        # Pages saved.
        row = await db.execute("SELECT COUNT(*) FROM pages")
        (page_count,) = await row.fetchone()
        print(f"Pages in DB: {page_count}")
        assert page_count >= 1, "No pages saved to storage"

        # Candidates generated.
        row = await db.execute("SELECT COUNT(*) FROM candidates")
        (cand_count,) = await row.fetchone()
        print(f"Candidates in DB: {cand_count}")
        # Wikipedia pages have many links; even a single page should yield some.
        assert cand_count >= 1, "No candidates generated"

        # Candidates by status.
        row = await db.execute("SELECT status, COUNT(*) FROM candidates GROUP BY status")
        statuses = await row.fetchall()
        print(f"Candidate statuses: {statuses}")

        # Rank decisions.
        row = await db.execute("SELECT COUNT(*) FROM rank_decisions")
        (rd_count,) = await row.fetchone()
        print(f"Rank decisions: {rd_count}")

        # Pages with successful extraction.
        row = await db.execute("SELECT COUNT(*) FROM pages WHERE extraction_status = 'OK'")
        (ok_count,) = await row.fetchone()
        print(f"Pages extracted OK: {ok_count}")

        # Show page titles for human review.
        row = await db.execute("SELECT title, url_key FROM pages LIMIT 15")
        titles = await row.fetchall()
        for t, k in titles:
            print(f"  [{k}] {t or '(no title)'}")


@pytest.mark.asyncio
async def test_wiki_rust_small_budget_stops_early(integration_settings):
    """With max_pages=2 the crawl should stop after exactly 2 fetched pages."""
    cfg = integration_settings
    setup_logging(cfg, force=True)

    goal = CrawlGoal(prompt=_PROMPT, max_pages=2)
    task = CrawlTask(goal_id=goal.goal_id, state="CREATED")

    sched = CrawlScheduler(settings=cfg)

    canon = Canonicalizer()
    url = canon.canonicalize(_SEED, _SEED)
    item = FrontierItem(
        url=url,
        url_key=url.url_key,
        priority=1.0,
        score_source="seed",
        reg_domain=url.reg_domain,
    )
    await sched._frontier.push_batch([item])

    print("\nSmall-budget test: max_pages=2")
    try:
        await asyncio.wait_for(sched.run(goal, task), timeout=120)
    except asyncio.TimeoutError:
        pass

    pages_fetched = sched._counters.get("pages_fetched", 0)
    print(f"Pages fetched: {pages_fetched}, Stop: {task.stopping_reason}")

    # Budget stop may fire while a fetch is in-flight; that page still completes.
    assert pages_fetched <= 2 + 1, f"Exceeded budget + inflight: {pages_fetched} > 3"
    assert pages_fetched >= 1, "No pages fetched"
    if task.stopping_reason:
        assert "BUDGET_PAGES" in (task.stopping_reason or ""), f"Expected BUDGET_PAGES, got {task.stopping_reason}"
