from __future__ import annotations

import asyncio

import pytest

from crawlme.state.storage import Storage


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def storage(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    db = tmp_path / "test.db"
    raw = tmp_path / "raw"
    raw.mkdir()
    s = Storage(str(db), str(raw))
    loop.run_until_complete(s.start())
    yield s
    loop.run_until_complete(s.close())
    loop.close()


def test_init_creates_all_tables(storage):
    tables = [
        "crawl_goals",
        "crawl_tasks",
        "urls",
        "pages",
        "candidates",
        "rank_decisions",
        "analyses",
        "feedback",
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


def test_save_and_get_url(storage):
    storage.save_url(
        {
            "url_key": "abc",
            "raw": "https://x.com",
            "canonical": "https://x.com",
            "domain": "x.com",
            "reg_domain": "x.com",
            "first_seen": "2026-01-01T00:00:00Z",
            "last_seen": "2026-01-01T00:00:00Z",
        }
    )
    _run(storage._write_queue.join())
    u = _run(storage.get_url("abc"))
    assert u is not None
    assert u["raw"] == "https://x.com"


def test_save_and_get_page(storage):
    storage.save_page(
        {
            "page_id": "p1",
            "url_key": "abc",
            "url_json": {"raw": "x"},
            "raw_html_path": "",
            "title": "Test Page",
            "extracted_at": "2026-01-01T00:00:00Z",
        }
    )
    _run(storage._write_queue.join())
    p = _run(storage.get_page("p1"))
    assert p is not None
    assert p["title"] == "Test Page"


def test_get_pages_by_url_key(storage):
    for i in range(3):
        storage.save_page(
            {
                "page_id": f"p{i}",
                "url_key": "abc",
                "url_json": {},
                "title": f"Page {i}",
                "extracted_at": f"2026-01-0{i + 1}T00:00:00Z",
            }
        )
    _run(storage._write_queue.join())
    pages = _run(storage.get_pages_by_url_key("abc"))
    assert len(pages) == 3


def test_save_raw_html(storage):
    path = storage.save_raw_html("abc", "f1", b"<html>hello</html>")
    assert path.endswith(".html")
    with open(path) as f:
        assert "hello" in f.read()


def test_save_and_get_candidate(storage):
    storage.save_candidate(
        {
            "candidate_id": "c1",
            "url_key": "abc",
            "url_json": {},
            "depth": 2,
            "status": "BUFFERED",
            "discovered_at": "2026-01-01T00:00:00Z",
        }
    )
    _run(storage._write_queue.join())
    c = _run(storage.get_candidate("c1"))
    assert c is not None
    assert c["depth"] == 2


def test_save_and_get_rank_decision(storage):
    storage.save_rank_decision(
        {
            "candidate_id": "c1",
            "url_key": "abc",
            "priority": 0.8,
            "ranker": "llm",
            "decided_at": "2026-01-01T00:00:00Z",
        }
    )
    _run(storage._write_queue.join())
    rd = _run(storage.get_rank_decision("c1"))
    assert rd is not None
    assert rd["priority"] == 0.8
    assert rd["ranker"] == "llm"


def test_get_rank_decisions_by_url_key(storage):
    for i in range(2):
        storage.save_rank_decision(
            {
                "candidate_id": f"c{i}",
                "url_key": "abc",
                "priority": 0.5 + i * 0.1,
                "decided_at": f"2026-01-0{i + 1}T00:00:00Z",
            }
        )
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
            "analyzed_at": "2026-01-01T00:00:00Z",
        }
    )
    _run(storage._write_queue.join())
    results = _run(storage.get_analyses_by_url_key("abc"))
    assert len(results) == 1
    assert results[0]["classification"] == "RELEVANT"


def test_save_and_get_feedback(storage):
    storage.save_feedback(
        {
            "reg_domain": "example.com",
            "hub_score": 0.75,
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )
    _run(storage._write_queue.join())
    fb = _run(storage.get_feedback("example.com"))
    assert fb is not None
    assert fb["hub_score"] == 0.75


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
    assert _run(storage.get_url("noexist")) is None
    assert _run(storage.get_page("noexist")) is None
