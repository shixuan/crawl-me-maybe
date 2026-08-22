"""Smoke crawls: broad, shallow, hermetic, and safe to run on every push.

Broad because they touch every layer from argv to SQLite. Shallow because
they assert that the machine ran and produced plausible rows, not that its
judgements are any good. That is the deal a smoke test makes, and it is
why these live here rather than in tests/e2e/: they crawl a site this
process serves to itself, with the LLM scripted or off, so nothing here
depends on the outside world.

The real end-to-end tests, against live Wikipedia, stay in tests/e2e/ and
stay out of CI.

Two levels, because they fail differently:

  1. The CLI in a subprocess, LLM off. Covers argument parsing, the
     factory, the whole pump, teardown, and the process exit code. That
     last one matters: a non-daemon aiosqlite worker thread once kept the
     interpreter alive after a run finished, and a log tail looks perfect
     when that happens.
  2. The pipeline in-process with a scripted LLM. Covers the stages the
     first one skips: goal enhancement, LLM re-ranking, page analysis,
     and the analyses they produce. No tokens are spent because no real
     client is ever built.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aiosqlite
import pytest

from crawlme.config import Settings
from crawlme.llm import LLMResponse
from crawlme.scheduler.factory import create_scheduler
from crawlme.schemas import URL, Candidate, CrawlGoal, CrawlTask

#: A small site shaped like a real focused crawl: a topical hub that
#: links to on-topic articles, one page the prefilter must drop, and one
#: extension the prefilter must drop. The vocabulary deliberately overlaps
#: the goal prompt, because that is what a hub reached from a good seed
#: looks like; unrelated wording would just measure the rule threshold.
_PROMPT = "memory safety and compiler tooling"

_ARTICLES = {
    "a1": (
        "Memory safety without a garbage collector",
        "Memory safety here means the compiler rejects use-after-free and data races before the program runs.",
    ),
    "a2": (
        "Compiler diagnostics that explain themselves",
        "Compiler tooling turns a type error into an explanation, which is a safety property of the toolchain.",
    ),
    "a3": (
        "Static analysis tooling for memory bugs",
        "Static analysis tooling finds memory errors without running the program, complementing compiler checks.",
    ),
    "a4": (
        "Borrow checking and memory ownership",
        "Ownership rules give memory safety at compile time, enforced by the compiler rather than at runtime.",
    ),
    "a5": (
        "Safety guarantees in systems tooling",
        "Systems tooling can offer memory safety guarantees when the compiler is allowed to reject unsound code.",
    ),
}


def _article_html(slug: str) -> bytes:
    title, body = _ARTICLES[slug]
    others = "".join(f'<a href="/{o}">{_ARTICLES[o][0]}</a> ' for o in _ARTICLES if o != slug)
    return (
        f"<!DOCTYPE html><html><head><title>{title}</title>"
        f'<meta property="article:published_time" content="2026-08-01T10:00:00Z"></head>'
        f"<body><nav><a href='/login'>Sign in</a></nav>"
        f"<article><h1>{title}</h1><p>{body}</p>"
        f"<p>{body} The paragraph continues so the extractor treats this as the main content.</p>"
        f"<h2>Related on memory safety and compiler tooling</h2><p>{others}</p></article>"
        f"<footer>Copyright 2024</footer></body></html>"
    ).encode()


def _index_html() -> bytes:
    links = "".join(f'<a href="/{s}">{_ARTICLES[s][0]}</a> ' for s in _ARTICLES)
    return (
        "<!DOCTYPE html><html><head><title>Memory safety and compiler tooling</title></head>"
        "<body><article><h1>Memory safety and compiler tooling</h1>"
        "<p>Writing about memory safety, compiler tooling and the static analysis that supports them.</p>"
        f"<p>{links}</p><p><a href='/login'>Sign in</a> <a href='/notes.pdf'>A PDF</a></p>"
        "</article></body></html>"
    ).encode()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?")[0].strip("/")
        if path == "robots.txt":
            return self._send(b"User-agent: *\nAllow: /\n", "text/plain")
        if path == "":
            return self._send(_index_html())
        if path in _ARTICLES:
            return self._send(_article_html(path))
        if path == "login":
            return self._send(b"<html><head><title>Sign in</title></head><body>form</body></html>")
        self.send_error(404)

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


@pytest.fixture
def site() -> object:
    """Serve the fixture site on a loopback port for the test's lifetime."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


async def _rows(db: Path, sql: str) -> list[tuple]:
    conn = await aiosqlite.connect(db)
    try:
        return list(await (await conn.execute(sql)).fetchall())
    finally:
        await conn.close()


def _only_run_db(result_dir: Path) -> Path:
    dbs = sorted(result_dir.glob("*/db/crawl.db"))
    assert len(dbs) == 1, f"expected exactly one run dir, found {dbs}"
    return dbs[0]


#: 1. the real CLI, in a real process ------------------------------------


def test_cli_run_completes_and_exits_clean(site: str, tmp_path: Path) -> None:
    """The assembled binary crawls a site and the process actually exits.

    Asserting on the exit code rather than on stdout is the point: a
    leaked non-daemon thread leaves a run that logged "finished" hanging
    forever, which no log assertion would catch.
    """
    result_dir = tmp_path / "results"
    # Args are literals plus sys.executable: nothing here is untrusted.
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "from crawlme.cli import main; main()",
            "run",
            _PROMPT,
            "--seeds",
            site,
            "--max-pages",
            "6",
            "--analysis",
            "off",
            "--result-dir",
            str(result_dir),
            "--log-level",
            "WARNING",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LLM_API_KEY": "", "LLM_BASE_URL": "", "LLM_MODEL": ""},
    )

    assert proc.returncode == 0, f"crawl exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"

    db = _only_run_db(result_dir)
    pages = asyncio.run(_rows(db, "SELECT url_json, title, extraction_status FROM pages"))
    assert len(pages) >= 3, f"crawled too few pages: {pages}"
    # The extractor's primary path must be the one that ran.
    assert {p[2] for p in pages} == {"OK"}, f"unexpected extraction statuses: {[p[2] for p in pages]}"
    assert all(p[1] and not p[1].startswith("http") for p in pages), (
        f"titles fell back to URLs: {[p[1] for p in pages]}"
    )

    links = asyncio.run(_rows(db, "SELECT status, count(*) FROM links GROUP BY status"))
    assert dict(links).get("BUFFERED", 0) > 0, f"no candidate survived the prefilter: {links}"

    decisions = asyncio.run(_rows(db, "SELECT ranker, count(*) FROM rank_decisions GROUP BY ranker"))
    # No credentials in the smoke environment, so there is no ranker and
    # every candidate passes through at one flat priority.  The rows
    # still have to appear: without them nothing is enqueued.
    assert dict(decisions).get("none", 0) > 0, f"nothing reached the frontier: {decisions}"

    events = {e[0] for e in asyncio.run(_rows(db, "SELECT DISTINCT type FROM events"))}
    assert {"FETCH_STARTED", "FETCH_COMPLETED", "PAGE_EXTRACTED"} <= events, events

    reason = asyncio.run(_rows(db, "SELECT state, stopping_reason FROM crawl_tasks"))[0]
    assert reason[0] == "COMPLETED", reason
    assert reason[1], "the task recorded no stopping reason"


def test_cli_rejects_a_bad_since_value(site: str, tmp_path: Path) -> None:
    """Argument validation fails before any crawling happens."""
    # Args are literals plus sys.executable: nothing here is untrusted.
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "from crawlme.cli import main; main()",
            "run",
            "anything",
            "--seeds",
            site,
            "--since",
            "whenever",
            "--result-dir",
            str(tmp_path / "results"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LLM_API_KEY": "", "LLM_BASE_URL": "", "LLM_MODEL": ""},
    )
    assert proc.returncode != 0
    assert "--since" in proc.stderr


#: 2. the LLM stages, with a scripted client ------------------------------


class _ScriptedLLM:
    """Stands in for LLMClient: same chat() shape, canned answers.

    Replies are chosen by looking for a marker in the prompt, so one
    client can serve the enhancer, the ranker, and the analyzer without
    the test caring what order they run in.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat(self, prompt: str, *, system: str = "", max_tokens: int = 512, json_mode: bool = False):
        self.calls.append(prompt)
        if "Candidate links" in prompt:
            ids = [line.split(":")[0] for line in prompt.splitlines() if line and line[0] == "c"]
            rankings = ", ".join(f'{{"id": "{i}", "priority": 0.9, "rationale": "scripted"}}' for i in ids)
            body = f'{{"rankings": [{rankings}], "candidates_to_drop": []}}'
        elif "classification" in prompt.lower() or "classification" in system.lower():
            body = json.dumps(
                {
                    "classification": "RELEVANT",
                    "relevance_score": 0.9,
                    "summary": "A scripted summary of the page.",
                    "tags": ["rust"],
                    "topics": ["memory safety"],
                    "entities": [],
                    "hub_score": 0.2,
                    "endorsed_links": [],
                }
            )
        else:
            body = json.dumps({"statement": "memory safety and compiler tooling", "keywords": ["memory", "safety"]})
        return LLMResponse(content=body, input_tokens=10, output_tokens=5, model="scripted")

    async def aclose(self) -> None:
        return None


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        result_dir=str(tmp_path / "results"),
        fetch_concurrency=2,
        ignore_robots=True,
        log_level="WARNING",
        # Pinned so a developer's .env can never turn this into a paid run.
        llm_api_key="",
        llm_base_url="",
        llm_model="",
    )


@pytest.mark.asyncio
async def test_pipeline_with_scripted_llm_produces_analyses(site: str, tmp_path: Path) -> None:
    """Analyzer and LLM ranker both run, on scripted answers."""
    from crawlme.analyzer import PageAnalyzer
    from crawlme.pioneer.ranker.llm import LLMRanker

    cfg = _settings(tmp_path)
    goal = CrawlGoal(prompt=_PROMPT, max_pages=5, domain_budget=20)
    task = CrawlTask(goal_id=goal.goal_id)

    client = _ScriptedLLM()
    analyzer = PageAnalyzer(client)  # type: ignore[arg-type]
    scheduler = create_scheduler(
        cfg,
        goal=goal,
        llm_ranker=LLMRanker(client),  # type: ignore[arg-type]
        analyzer=analyzer,
    )

    seed = Candidate(url=URL(raw=site, canonical=site, url_key=site), discovered_at=datetime.now(timezone.utc))
    assert await scheduler.ingest_seeds(goal, [seed]) == 1
    await scheduler.run(goal, task)
    await scheduler.aclose()

    db = _only_run_db(tmp_path / "results")

    analyses = await _rows(db, "SELECT classification, relevance_score, summary, model FROM analyses")
    assert analyses, "the analyzer produced nothing"
    assert {a[0] for a in analyses} == {"RELEVANT"}
    assert all(a[2] for a in analyses), "analyses stored without a summary"

    rankers = dict(await _rows(db, "SELECT ranker, count(*) FROM rank_decisions GROUP BY ranker"))
    assert rankers.get("llm", 0) > 0, f"the LLM stage never ranked: {rankers}"

    assert client.calls, "no scripted LLM call was made"
    assert task.state == "COMPLETED", task.stopping_reason


@pytest.mark.asyncio
async def test_pipeline_degrades_without_llm_credentials(site: str, tmp_path: Path) -> None:
    """No credentials is a supported configuration, not a failure.

    The crawl still has to complete and still has to store pages and rule
    decisions; only the LLM-fed tables stay empty.
    """
    cfg = _settings(tmp_path)
    goal = CrawlGoal(prompt=_PROMPT, max_pages=4, domain_budget=20)
    task = CrawlTask(goal_id=goal.goal_id)

    scheduler = create_scheduler(cfg, goal=goal)
    seed = Candidate(url=URL(raw=site, canonical=site, url_key=site), discovered_at=datetime.now(timezone.utc))
    await scheduler.ingest_seeds(goal, [seed])
    await scheduler.run(goal, task)
    await scheduler.aclose()

    db = _only_run_db(tmp_path / "results")
    assert await _rows(db, "SELECT 1 FROM pages"), "no pages were stored"
    assert not await _rows(db, "SELECT 1 FROM analyses"), "analyses appeared without an analyzer"
    assert task.state == "COMPLETED", task.stopping_reason
