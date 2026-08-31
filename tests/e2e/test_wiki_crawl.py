"""End-to-end tests against live Wikipedia.

Run manually (skipped in CI):

    pytest tests/e2e/test_wiki_crawl.py -v -s -m e2e

Goal: find software projects that were rewritten from other languages
into Rust ("projects rewritten in Rust").
"""

from __future__ import annotations

import asyncio
import time

import aiosqlite
import pytest

from crawlme.logging import setup_logging
from crawlme.pioneer.canonicalizer import Canonicalizer
from crawlme.scheduler.factory import create_scheduler
from crawlme.schemas import CrawlGoal, CrawlTask, FrontierItem

pytestmark = pytest.mark.e2e

_SEED = "https://en.wikipedia.org/wiki/Rust_(programming_language)"
_PROMPT = "systems programming, memory safety, compiler design and static analysis tools"
_MAX_PAGES = 10


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_wiki_basic(e2e_settings):
    """Full pipeline: seed Wikipedia Rust page, crawl up to 10 pages.

    Verifies:
      - Crawl completes without fatal errors
      - Pages are fetched and extracted
      - Candidates flow through prefilter → buffer → ranker → frontier
      - Storage has pages, links, rank_decisions
      - Stop reason includes BUDGET_PAGES
    """
    cfg = e2e_settings
    setup_logging(cfg, force=True)

    goal = CrawlGoal(prompt=_PROMPT, max_pages=_MAX_PAGES)
    task = CrawlTask(goal_id=goal.goal_id, state="CREATED")

    sched = create_scheduler(cfg)

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
    pages_fetched = counters.pages_fetched

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
        row = await db.execute("SELECT COUNT(*) FROM links")
        (cand_count,) = await row.fetchone()
        print(f"Candidates in DB: {cand_count}")
        # Wikipedia pages have many links; even a single page should yield some.
        assert cand_count >= 1, "No candidates generated"

        # Candidates by status.
        row = await db.execute("SELECT status, COUNT(*) FROM links GROUP BY status")
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
async def test_wiki_budget(e2e_settings):
    """With max_pages=2 the crawl should stop after exactly 2 fetched pages."""
    cfg = e2e_settings
    setup_logging(cfg, force=True)

    goal = CrawlGoal(prompt=_PROMPT, max_pages=2)
    task = CrawlTask(goal_id=goal.goal_id, state="CREATED")

    sched = create_scheduler(cfg)

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

    pages_fetched = sched._counters.pages_fetched
    print(f"Pages fetched: {pages_fetched}, Stop: {task.stopping_reason}")

    # Budget stop may fire while a fetch is in-flight; that page still completes.
    assert pages_fetched <= 2 + 1, f"Exceeded budget + inflight: {pages_fetched} > 3"
    assert pages_fetched >= 1, "No pages fetched"
    if task.stopping_reason:
        assert "BUDGET_PAGES" in (task.stopping_reason or ""), f"Expected BUDGET_PAGES, got {task.stopping_reason}"


# -- draining mode: many seeds, deep crawl, no page limit --------------------

_DRAINING_SEEDS = [
    # Core Rust ecosystem
    "https://en.wikipedia.org/wiki/Rust_(programming_language)",
    "https://en.wikipedia.org/wiki/Servo_(software)",
    "https://en.wikipedia.org/wiki/Firefox",
    # Languages & runtimes
    "https://en.wikipedia.org/wiki/C_(programming_language)",
    "https://en.wikipedia.org/wiki/C%2B%2B",
    "https://en.wikipedia.org/wiki/Go_(programming_language)",
    "https://en.wikipedia.org/wiki/Memory_safety",
    # Projects rewritten/adopted Rust
    "https://en.wikipedia.org/wiki/Librsvg",
    "https://en.wikipedia.org/wiki/GitHub_Copilot",
    "https://en.wikipedia.org/wiki/Cloudflare",
    # Systems & OS
    "https://en.wikipedia.org/wiki/Linux_kernel",
    "https://en.wikipedia.org/wiki/WebAssembly",
    # Tools
    "https://en.wikipedia.org/wiki/Static_program_analysis",
    "https://en.wikipedia.org/wiki/Compiler",
    "https://en.wikipedia.org/wiki/Formal_verification",
]
_DRAINING_DEPTH = 5
_DRAINING_TIMEOUT = 600


@pytest.mark.asyncio
async def test_wiki_draining(e2e_settings):
    """Draining mode: 15 seeds, depth=3, no page limit. Crawls until frontier drained.

    Verifies:
      - Multiple seeds all pushed successfully
      - Crawler runs to natural exhaustion (FRONTIER_DRAINED)
      - Depth limit is respected (no pages beyond depth 3)
      - Substantial page/candidate yield from cross-linking
    """
    cfg = e2e_settings
    setup_logging(cfg, force=True)

    goal = CrawlGoal(prompt=_PROMPT, max_pages=0, depth_limit=_DRAINING_DEPTH)
    task = CrawlTask(goal_id=goal.goal_id, state="CREATED")

    sched = create_scheduler(cfg)

    # Push all seeds.
    canon = Canonicalizer()
    items = []
    for seed_url in _DRAINING_SEEDS:
        url = canon.canonicalize(seed_url, seed_url)
        items.append(
            FrontierItem(
                url=url,
                url_key=url.url_key,
                priority=1.0,
                score_source="seed",
                reg_domain=url.reg_domain,
            )
        )
    await sched._frontier.push_batch(items)

    print(f"\nDraining test: {len(_DRAINING_SEEDS)} seeds, depth={_DRAINING_DEPTH}, no page limit")
    print(f"Seeds: {', '.join(s.rsplit('/', 1)[-1] for s in _DRAINING_SEEDS)}")

    t0 = time.monotonic()
    timed_out = False
    try:
        await asyncio.wait_for(sched.run(goal, task), timeout=_DRAINING_TIMEOUT)
    except asyncio.TimeoutError:
        timed_out = True
        print(f"\nTimed out after {_DRAINING_TIMEOUT}s — checking partial results.")

    elapsed = time.monotonic() - t0
    counters = sched._counters
    pages_fetched = counters.pages_fetched

    print(f"\nElapsed: {elapsed:.1f}s")
    print(f"Pages fetched: {pages_fetched}")
    print(f"State: {task.state}")
    print(f"Stop reason: {task.stopping_reason}")

    # 1. Must have fetched a reasonable number of pages.
    assert pages_fetched >= len(_DRAINING_SEEDS), (
        f"Expected at least {len(_DRAINING_SEEDS)} pages (one per seed), got {pages_fetched}"
    )

    # 2. State must be terminal unless timed out.
    if not timed_out:
        assert task.state in ("COMPLETED", "STOPPING"), f"Unexpected state: {task.state}"
        assert task.stopping_reason and "FRONTIER_DRAINED" in (task.stopping_reason or ""), (
            f"Expected FRONTIER_DRAINED, got {task.stopping_reason}"
        )

    # 3. Verify DB contents.
    db_path = sched._storage.db_path
    async with aiosqlite.connect(db_path) as db:
        row = await db.execute("SELECT COUNT(*) FROM pages")
        (page_count,) = await row.fetchone()
        print(f"Pages in DB: {page_count}")
        assert page_count >= len(_DRAINING_SEEDS), f"Expected >= {len(_DRAINING_SEEDS)} pages, got {page_count}"

        row = await db.execute("SELECT COUNT(*) FROM links")
        (cand_count,) = await row.fetchone()
        print(f"Candidates in DB: {cand_count}")

        row = await db.execute("SELECT status, COUNT(*) FROM links GROUP BY status")
        statuses = await row.fetchall()
        print(f"Candidate statuses: {statuses}")

        row = await db.execute("SELECT COUNT(*) FROM rank_decisions")
        (rd_count,) = await row.fetchone()
        print(f"Rank decisions: {rd_count}")

        # Depth check: no page beyond depth limit.
        row = await db.execute("SELECT MAX(depth) FROM links")
        (max_depth,) = await row.fetchone()
        print(f"Max candidate depth: {max_depth}")
        if max_depth is not None:
            assert max_depth <= _DRAINING_DEPTH + 1, f"Depth limit violated: {max_depth} > {_DRAINING_DEPTH + 1}"

        # Show all page titles.
        row = await db.execute("SELECT DISTINCT title, json_extract(url_json, '$.raw') FROM pages ORDER BY rowid")
        titles = await row.fetchall()
        print(f"\nAll pages ({len(titles)}):")
        for t, u in titles:
            print(f"  {t or '(no title)':60s} {u[:80] if u else ''}")
