from __future__ import annotations

import asyncio
import datetime

import pytest

from crawlme.schemas import URL, Candidate, Page, RankDecision
from crawlme.storage.sqlite.crawl_db import SqliteCrawlDb


def _url(url_key: str = "abc") -> URL:
    return URL(raw="https://x.com", canonical="https://x.com", url_key=url_key, reg_domain="example.com")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def storage(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    db = tmp_path / "test.db"
    raw = tmp_path / "raw"
    raw.mkdir()
    s = SqliteCrawlDb(str(db), str(raw))
    loop.run_until_complete(s.start())
    yield s
    loop.run_until_complete(s.close())
    loop.close()


def test_init_creates_all_tables(storage):
    tables = [
        "crawl_goals",
        "crawl_tasks",
        "pages",
        "candidates",
        "rank_decisions",
        "analyses",
        "frontier_snapshots",
        "events",
        "errors",
        "robots_cache",
    ]
    for t in tables:
        cur = _run(storage._execute_now("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)))
        row = _run(cur.fetchone())
        assert row is not None, f"Table {t} missing"


def test_save_and_get_goal(storage):
    storage.save_goal(
        {
            "goal_id": "g1",
            "prompt": "find AI news",
            "max_pages": 10,
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    _run(storage._write_queue.join())
    g = _run(storage.get_goal("g1"))
    assert g is not None
    assert g["prompt"] == "find AI news"
    assert g["max_pages"] == 10


def test_save_goal_roundtrips_enhanced_fields(storage):
    """The Goal Enhancer's keywords and since survive persistence."""
    storage.save_goal(
        {
            "goal_id": "g2",
            "prompt": "最近半年的LLM推理框架进展",
            "goal_statement": "Find recent progress on LLM inference frameworks / 找最近LLM推理框架进展",
            "keywords": ["llm", "inference", "vllm"],
            "since": "2026-02-13T00:00:00+00:00",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    _run(storage._write_queue.join())
    g = _run(storage.get_goal("g2"))
    assert g is not None
    assert g["goal_statement"].startswith("Find recent progress")
    assert '"vllm"' in g["keywords"]
    assert g["since"] == "2026-02-13T00:00:00+00:00"


def test_save_and_get_task(storage):
    storage.save_task(
        {
            "task_id": "t1",
            "goal_id": "g1",
            "state": "RUNNING",
            "counters": {},
            "start_at": "2026-01-01T00:00:00Z",
        }
    )
    _run(storage._write_queue.join())
    t = _run(storage.get_task("t1"))
    assert t is not None
    assert t["state"] == "RUNNING"


def test_save_and_get_page(storage):
    url = _url("abc")
    storage.save_page(Page(page_id="p1", url_key="abc", url=url, title="Test Page"))
    _run(storage._write_queue.join())
    p = _run(storage.get_page("p1"))
    assert p is not None
    assert p["title"] == "Test Page"


def test_get_pages_by_url_key(storage):
    url = _url("abc")
    for i in range(3):
        storage.save_page(Page(page_id=f"p{i}", url_key="abc", url=url, title=f"Page {i}"))
    _run(storage._write_queue.join())
    pages = _run(storage.get_pages_by_url_key("abc"))
    assert len(pages) == 3


def test_list_pages_returns_all_in_fetch_order(storage):
    for i in (2, 0, 1):
        storage.save_page(
            Page(
                page_id=f"p{i}",
                url_key=f"k{i}",
                url=_url(f"k{i}"),
                extracted_at=datetime.datetime(2026, 1, 1, 0, 0, i),
            )
        )
    _run(storage._write_queue.join())
    pages = _run(storage.list_pages())
    assert [p["page_id"] for p in pages] == ["p0", "p1", "p2"]


def test_save_raw_html(storage):
    path = storage.save_raw_html("abc", "f1", b"<html>hello</html>")
    assert path.endswith(".html")
    with open(path) as f:
        assert "hello" in f.read()


def test_save_and_get_candidate(storage):
    storage.save_candidate(Candidate(candidate_id="c1", url=_url("abc"), depth=2, status="BUFFERED"))
    _run(storage._write_queue.join())
    c = _run(storage.get_candidate("c1"))
    assert c is not None
    assert c["depth"] == 2


def test_save_and_get_rank_decision(storage):
    storage.save_rank_decision(RankDecision(candidate_id="c1", url_key="abc", priority=0.8, ranker="llm"))
    _run(storage._write_queue.join())
    rd = _run(storage.get_rank_decision("c1"))
    assert rd is not None
    assert rd["priority"] == 0.8
    assert rd["ranker"] == "llm"


def test_get_rank_decisions_by_url_key(storage):
    for i in range(2):
        storage.save_rank_decision(RankDecision(candidate_id=f"c{i}", url_key="abc", priority=0.5 + i * 0.1))
    _run(storage._write_queue.join())
    rds = _run(storage.get_rank_decisions_by_url_key("abc"))
    assert len(rds) == 2


def test_save_and_get_analyses(storage):
    storage.save_analysis(
        {
            "analysis_id": "a1",
            "page_id": "p1",
            "url_key": "abc",
            "classification": "RELEVANT",
            "feedback": {"classification": "RELEVANT", "hub_score": 0.6, "endorsed_links": ["https://x.com/y"]},
            "analyzed_at": "2026-01-01T00:00:00Z",
        }
    )
    _run(storage._write_queue.join())
    results = _run(storage.get_analyses_by_url_key("abc"))
    assert len(results) == 1
    assert results[0]["classification"] == "RELEVANT"
    # The scheduler-facing feedback signals must survive persistence.
    assert '"hub_score": 0.6' in results[0]["feedback_json"]
    assert "https://x.com/y" in results[0]["feedback_json"]


def test_has_analysis_matches_identity(storage):
    storage.save_analysis(
        {
            "analysis_id": "a1",
            "url_key": "abc",
            "goal_id": "g1",
            "prompt_version": "v1",
            "model": "m1",
            "analyzed_at": "2026-01-01T00:00:00Z",
        }
    )
    _run(storage._write_queue.join())

    assert _run(storage.has_analysis("abc", "g1", "v1"))  # "" matches any model
    assert _run(storage.has_analysis("abc", "g1", "v1", "m1"))
    assert not _run(storage.has_analysis("abc", "g1", "v1", "m2"))
    assert not _run(storage.has_analysis("abc", "g1", "v2"))
    assert not _run(storage.has_analysis("abc", "g2", "v1"))
    assert not _run(storage.has_analysis("other", "g1", "v1"))


def test_save_and_get_snapshot(storage):
    storage.save_snapshot(
        {
            "snapshot_id": "s1",
            "task_id": "t1",
            "snapshot_json": {"heap": [1, 2, 3]},
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    _run(storage._write_queue.join())
    snap = _run(storage.get_snapshot("s1"))
    assert snap is not None


def test_save_and_get_events(storage):
    storage.save_event(
        {
            "ts": "2026-01-01T00:00:00Z",
            "task_id": "t1",
            "type": "PAGE_FETCHED",
            "payload": {},
        }
    )
    storage.save_event(
        {
            "ts": "2026-01-01T00:00:01Z",
            "task_id": "t1",
            "type": "PAGE_ANALYZED",
            "payload": {},
        }
    )
    _run(storage._write_queue.join())
    events = _run(storage.get_events_after("t1", 0))
    assert len(events) == 2
    assert events[0]["type"] == "PAGE_FETCHED"


def test_save_and_get_errors(storage):
    storage.save_error(
        {
            "task_id": "t1",
            "url_key": "abc",
            "stage": "fetch",
            "error_type": "timeout",
            "attempt": 2,
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    _run(storage._write_queue.join())
    errors = _run(storage.get_errors_by_task("t1"))
    assert len(errors) == 1
    assert errors[0]["stage"] == "fetch"
    assert errors[0]["error_type"] == "timeout"

    by_url = _run(storage.get_errors_by_url_key("abc"))
    assert len(by_url) == 1


def test_save_and_get_robots(storage):
    storage.save_robots(
        {
            "domain": "example.com",
            "raw": "User-agent: *\nDisallow: /",
            "fetched_at": "2026-01-01T00:00:00Z",
        }
    )
    _run(storage._write_queue.join())
    r = _run(storage.get_robots("example.com"))
    assert r is not None
    assert "Disallow" in r["raw"]


def test_get_nonexistent(storage):
    assert _run(storage.get_goal("noexist")) is None
    assert _run(storage.get_page("noexist")) is None
