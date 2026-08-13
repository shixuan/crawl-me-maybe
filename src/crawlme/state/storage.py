from __future__ import annotations

import asyncio
import datetime
import json
from array import array
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import aiosqlite

if TYPE_CHECKING:
    from crawlme.schemas import Candidate, Page, RankDecision

DDL = """
CREATE TABLE IF NOT EXISTS crawl_goals (
    goal_id    TEXT PRIMARY KEY,
    prompt     TEXT NOT NULL,
    goal_statement TEXT DEFAULT '',
    embedding  TEXT,
    max_pages  INTEGER DEFAULT 500,
    max_tokens INTEGER DEFAULT 2000000,
    max_duration_sec INTEGER DEFAULT 3600,
    min_relevant_hits INTEGER DEFAULT 3,
    relevance_threshold REAL DEFAULT 0.7,
    depth_limit INTEGER DEFAULT 5,
    domain_budget INTEGER DEFAULT 50,
    extraction_spec TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_tasks (
    task_id      TEXT PRIMARY KEY,
    goal_id      TEXT NOT NULL,
    state        TEXT DEFAULT 'CREATED',
    counters     TEXT DEFAULT '{}',
    start_at     TEXT NOT NULL,
    end_at       TEXT,
    stopping_reason TEXT,
    checkpoint_ref TEXT
);

CREATE TABLE IF NOT EXISTS urls (
    url_key    TEXT PRIMARY KEY,
    raw        TEXT NOT NULL,
    canonical  TEXT NOT NULL,
    scheme     TEXT DEFAULT '',
    host       TEXT DEFAULT '',
    path       TEXT DEFAULT '',
    query      TEXT DEFAULT '',
    domain     TEXT DEFAULT '',
    reg_domain TEXT DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    status     TEXT DEFAULT 'NEW'
);

CREATE TABLE IF NOT EXISTS pages (
    page_id          TEXT PRIMARY KEY,
    url_key          TEXT NOT NULL,
    url_json         TEXT NOT NULL,
    raw_html_path    TEXT DEFAULT '',
    title            TEXT,
    markdown         TEXT,
    plain_text       TEXT,
    metadata_json    TEXT DEFAULT '{}',
    text_hash        TEXT DEFAULT '',
    text_len         INTEGER DEFAULT 0,
    extracted_at     TEXT NOT NULL,
    extraction_status TEXT DEFAULT 'OK'
);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id    TEXT PRIMARY KEY,
    url_key         TEXT NOT NULL,
    url_json        TEXT NOT NULL,
    anchor          TEXT,
    snippet         TEXT,
    parent_heading  TEXT,
    position        INTEGER DEFAULT 0,
    source_page_id  TEXT,
    source_url_key  TEXT,
    depth           INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'INGESTED',
    discovered_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rank_decisions (
    candidate_id   TEXT PRIMARY KEY,
    url_key        TEXT DEFAULT '',
    priority       REAL DEFAULT 0.0,
    dropped        INTEGER DEFAULT 0,
    rationale      TEXT,
    ranker         TEXT DEFAULT 'rule',
    tokens_used    INTEGER DEFAULT 0,
    decided_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    analysis_id     TEXT PRIMARY KEY,
    page_id         TEXT DEFAULT '',
    url_key         TEXT DEFAULT '',
    goal_id         TEXT DEFAULT '',
    classification  TEXT DEFAULT 'UNKNOWN',
    relevance_score REAL DEFAULT 0.0,
    summary         TEXT,
    structured_data TEXT DEFAULT '{}',
    tags_json       TEXT DEFAULT '[]',
    feedback_json   TEXT DEFAULT '{}',
    model           TEXT DEFAULT '',
    prompt_version  TEXT DEFAULT '',
    tokens_used     INTEGER DEFAULT 0,
    analyzed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    reg_domain     TEXT PRIMARY KEY,
    hub_score      REAL DEFAULT 0.0,
    relevance_agg  TEXT DEFAULT '{}',
    topics_json    TEXT DEFAULT '[]',
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS frontier_snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    task_id      TEXT DEFAULT '',
    snapshot_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    task_id     TEXT DEFAULT '',
    type        TEXT NOT NULL,
    payload_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS errors (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT DEFAULT '',
    url_key      TEXT DEFAULT '',
    stage        TEXT NOT NULL,
    error_type   TEXT NOT NULL,
    attempt      INTEGER DEFAULT 0,
    next_retry_at TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS robots_cache (
    domain     TEXT PRIMARY KEY,
    raw        TEXT DEFAULT '',
    fetched_at TEXT NOT NULL,
    ttl        INTEGER DEFAULT 86400
);

CREATE TABLE IF NOT EXISTS embeddings (
    content_hash TEXT PRIMARY KEY,
    model        TEXT DEFAULT '',
    dims         INTEGER DEFAULT 0,
    vector       BLOB,
    created_at   TEXT NOT NULL
);
"""


class Storage(Protocol):
    """Contract for persistent state: SQLite today, Postgres tomorrow."""

    @property
    def db_path(self) -> str: ...

    async def start(self) -> None: ...
    async def close(self) -> None: ...

    def raw_html_path(self, url_key: str, fetch_id: str) -> str: ...
    def save_raw_html(self, url_key: str, fetch_id: str, content: bytes) -> str: ...

    def save_page(self, page: Page) -> None: ...
    def save_candidate(self, candidate: Candidate) -> None: ...
    def save_rank_decision(self, rd: RankDecision) -> None: ...
    def save_snapshot(self, snapshot_json: dict[str, Any]) -> None: ...
    def save_event(self, event_json: dict[str, Any]) -> None: ...

    async def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None: ...


class SqliteStorage:
    def __init__(self, db_path: str, raw_dir: str):
        self._db_path = db_path
        self._raw_dir = Path(raw_dir)
        self._write_queue: asyncio.Queue[tuple[str, tuple[Any, ...]]] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    def create(cls, base_dir: str | Path) -> SqliteStorage:
        """Create a Storage with a timestamped subdirectory under *base_dir*.

        Each crawl gets an isolated directory: ``base_dir/YYYYMMDD_HHMMSS/``
        """
        import datetime

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = Path(base_dir) / ts
        (run_dir / "raw").mkdir(parents=True, exist_ok=True)
        (run_dir / "db").mkdir(parents=True, exist_ok=True)
        return cls(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def raw_dir(self) -> Path:
        return self._raw_dir

    async def start(self) -> None:
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self._db_path)
        # Log to a file inside the run directory (setup_logging runs first).
        from crawlme.logging import to_file

        to_file(str(Path(self._db_path).parent.parent / "log"))
        await self._conn.executescript(DDL)
        await self._conn.commit()
        self._conn.row_factory = aiosqlite.Row
        self._writer_task = asyncio.create_task(self._write_loop())

    async def close(self) -> None:
        if self._writer_task:
            await self._write_queue.join()
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
        if self._conn:
            # Final commit: flushes any writes since the last batch commit.
            await self._conn.commit()
            await self._conn.close()

    async def _write_loop(self) -> None:
        batch = 0
        while True:
            sql, params = await self._write_queue.get()
            try:
                if self._conn:
                    await self._conn.execute(sql, params)
                    batch += 1
                    # Commit every 200 writes to avoid thrashing SQLite.
                    if batch >= 200:
                        await self._conn.commit()
                        batch = 0
            finally:
                self._write_queue.task_done()

    def _enqueue_write(self, sql: str, params: tuple[Any, ...]) -> None:
        self._write_queue.put_nowait((sql, params))

    async def _execute_now(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        assert self._conn is not None
        return await self._conn.execute(sql, params)

    #: raw HTML -----------------------------------------------------------

    def raw_html_path(self, url_key: str, fetch_id: str) -> str:
        return str(self._raw_dir / url_key / f"{fetch_id}.html")

    def save_raw_html(self, url_key: str, fetch_id: str, content: bytes) -> str:
        path = Path(self.raw_html_path(url_key, fetch_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return str(path)

    #: crawl_goals --------------------------------------------------------

    def save_goal(self, goal_json: dict[str, Any]) -> None:
        self._enqueue_write(
            "INSERT OR REPLACE INTO crawl_goals(goal_id, prompt, goal_statement, embedding, "
            "max_pages, max_tokens, max_duration_sec, min_relevant_hits, "
            "relevance_threshold, depth_limit, domain_budget, extraction_spec, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                goal_json["goal_id"],
                goal_json["prompt"],
                goal_json.get("goal_statement", ""),
                json.dumps(goal_json.get("embedding")),
                goal_json.get("max_pages", 500),
                goal_json.get("max_tokens", 2_000_000),
                goal_json.get("max_duration_sec", 3600),
                goal_json.get("min_relevant_hits", 3),
                goal_json.get("relevance_threshold", 0.7),
                goal_json.get("depth_limit", 5),
                goal_json.get("domain_budget", 50),
                json.dumps(goal_json.get("extraction_spec")),
                goal_json.get("created_at", ""),
            ),
        )

    async def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        cur = await self._execute_now("SELECT * FROM crawl_goals WHERE goal_id = ?", (goal_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    #: crawl_tasks --------------------------------------------------------

    def save_task(self, task_json: dict[str, Any]) -> None:
        self._enqueue_write(
            "INSERT OR REPLACE INTO crawl_tasks(task_id, goal_id, state, counters, "
            "start_at, end_at, stopping_reason, checkpoint_ref) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_json["task_id"],
                task_json.get("goal_id", ""),
                task_json.get("state", "CREATED"),
                json.dumps(task_json.get("counters", {})),
                task_json.get("start_at", ""),
                task_json.get("end_at"),
                task_json.get("stopping_reason"),
                task_json.get("checkpoint_ref"),
            ),
        )

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        cur = await self._execute_now("SELECT * FROM crawl_tasks WHERE task_id = ?", (task_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    #: urls ---------------------------------------------------------------

    def save_url(self, url_json: dict[str, Any]) -> None:
        self._enqueue_write(
            "INSERT OR REPLACE INTO urls(url_key, raw, canonical, scheme, host, "
            "path, query, domain, reg_domain, first_seen, last_seen, status) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                url_json["url_key"],
                url_json["raw"],
                url_json["canonical"],
                url_json.get("scheme", ""),
                url_json.get("host", ""),
                url_json.get("path", ""),
                url_json.get("query", ""),
                url_json.get("domain", ""),
                url_json.get("reg_domain", ""),
                url_json.get("first_seen", url_json.get("last_seen", "")),
                url_json.get("last_seen", ""),
                url_json.get("status", "NEW"),
            ),
        )

    async def get_url(self, url_key: str) -> dict[str, Any] | None:
        cur = await self._execute_now("SELECT * FROM urls WHERE url_key = ?", (url_key,))
        row = await cur.fetchone()
        return dict(row) if row else None

    #: pages --------------------------------------------------------------

    def save_page(self, page: Page) -> None:
        self._enqueue_write(
            "INSERT OR REPLACE INTO pages(page_id, url_key, url_json, raw_html_path, "
            "title, markdown, plain_text, metadata_json, text_hash, text_len, "
            "extracted_at, extraction_status) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                page.page_id,
                page.url_key,
                json.dumps(page.url.model_dump()),
                page.raw_html_path,
                page.title,
                page.markdown,
                page.plain_text,
                json.dumps(page.metadata),
                page.text_hash,
                page.text_len,
                page.extracted_at.isoformat() if page.extracted_at else "",
                page.extraction_status,
            ),
        )

    async def get_page(self, page_id: str) -> dict[str, Any] | None:
        cur = await self._execute_now("SELECT * FROM pages WHERE page_id = ?", (page_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_pages_by_url_key(self, url_key: str) -> list[dict[str, Any]]:
        cur = await self._execute_now("SELECT * FROM pages WHERE url_key = ? ORDER BY extracted_at", (url_key,))
        return [dict(r) for r in await cur.fetchall()]

    #: candidates ---------------------------------------------------------

    def save_candidate(self, candidate: Candidate) -> None:
        self._enqueue_write(
            "INSERT OR REPLACE INTO candidates(candidate_id, url_key, url_json, "
            "anchor, snippet, parent_heading, position, source_page_id, "
            "source_url_key, depth, status, discovered_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate.candidate_id,
                candidate.url.url_key,
                json.dumps(candidate.url.model_dump()),
                candidate.anchor,
                candidate.snippet,
                candidate.parent_heading,
                candidate.position,
                candidate.source_page_id,
                candidate.source_url_key,
                candidate.depth,
                candidate.status,
                candidate.discovered_at.isoformat() if candidate.discovered_at else "",
            ),
        )

    async def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        cur = await self._execute_now("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    #: rank_decisions -----------------------------------------------------

    def save_rank_decision(self, rd: RankDecision) -> None:
        self._enqueue_write(
            "INSERT OR REPLACE INTO rank_decisions(candidate_id, url_key, priority, "
            "dropped, rationale, ranker, tokens_used, decided_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rd.candidate_id,
                rd.url_key,
                rd.priority,
                1 if rd.dropped else 0,
                rd.rationale,
                rd.ranker,
                rd.tokens_used,
                rd.decided_at.isoformat(),
            ),
        )

    async def get_rank_decision(self, candidate_id: str) -> dict[str, Any] | None:
        cur = await self._execute_now("SELECT * FROM rank_decisions WHERE candidate_id = ?", (candidate_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_rank_decisions_by_url_key(self, url_key: str) -> list[dict[str, Any]]:
        cur = await self._execute_now("SELECT * FROM rank_decisions WHERE url_key = ? ORDER BY decided_at", (url_key,))
        return [dict(r) for r in await cur.fetchall()]

    #: analyses -----------------------------------------------------------

    def save_analysis(self, analysis_json: dict[str, Any]) -> None:
        self._enqueue_write(
            "INSERT OR REPLACE INTO analyses(analysis_id, page_id, url_key, goal_id, "
            "classification, relevance_score, summary, structured_data, tags_json, "
            "feedback_json, model, prompt_version, tokens_used, analyzed_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                analysis_json["analysis_id"],
                analysis_json.get("page_id", ""),
                analysis_json.get("url_key", ""),
                analysis_json.get("goal_id", ""),
                analysis_json.get("classification", "UNKNOWN"),
                analysis_json.get("relevance_score", 0.0),
                analysis_json.get("summary"),
                json.dumps(analysis_json.get("structured_data", {})),
                json.dumps(analysis_json.get("tags", [])),
                json.dumps(analysis_json.get("feedback_json", {})),
                analysis_json.get("model", ""),
                analysis_json.get("prompt_version", ""),
                analysis_json.get("tokens_used", 0),
                analysis_json.get("analyzed_at", ""),
            ),
        )

    async def get_analyses_by_url_key(self, url_key: str) -> list[dict[str, Any]]:
        cur = await self._execute_now("SELECT * FROM analyses WHERE url_key = ? ORDER BY analyzed_at", (url_key,))
        return [dict(r) for r in await cur.fetchall()]

    #: feedback -----------------------------------------------------------

    def save_feedback(self, fb_json: dict[str, Any]) -> None:
        self._enqueue_write(
            "INSERT OR REPLACE INTO feedback(reg_domain, hub_score, relevance_agg, "
            "topics_json, updated_at) VALUES(?, ?, ?, ?, ?)",
            (
                fb_json["reg_domain"],
                fb_json.get("hub_score", 0.0),
                json.dumps(fb_json.get("relevance_agg", {})),
                json.dumps(fb_json.get("topics", [])),
                fb_json.get("updated_at", ""),
            ),
        )

    async def get_feedback(self, reg_domain: str) -> dict[str, Any] | None:
        cur = await self._execute_now("SELECT * FROM feedback WHERE reg_domain = ?", (reg_domain,))
        row = await cur.fetchone()
        return dict(row) if row else None

    #: frontier_snapshots -------------------------------------------------

    def save_snapshot(self, snapshot_json: dict[str, Any]) -> None:
        self._enqueue_write(
            "INSERT OR REPLACE INTO frontier_snapshots(snapshot_id, task_id, "
            "snapshot_json, created_at) VALUES(?, ?, ?, ?)",
            (
                snapshot_json["snapshot_id"],
                snapshot_json.get("task_id", ""),
                json.dumps(snapshot_json.get("snapshot_json", {})),
                snapshot_json.get("created_at", ""),
            ),
        )

    async def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        cur = await self._execute_now("SELECT * FROM frontier_snapshots WHERE snapshot_id = ?", (snapshot_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    #: events -------------------------------------------------------------

    def save_event(self, event_json: dict[str, Any]) -> None:
        self._enqueue_write(
            "INSERT INTO events(ts, task_id, type, payload_json) VALUES(?, ?, ?, ?)",
            (
                event_json.get("ts", ""),
                event_json.get("task_id", ""),
                event_json["type"],
                json.dumps(event_json.get("payload", {})),
            ),
        )

    async def get_events_after(self, task_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        cur = await self._execute_now(
            "SELECT * FROM events WHERE task_id = ? AND seq > ? ORDER BY seq",
            (task_id, after_seq),
        )
        return [dict(r) for r in await cur.fetchall()]

    #: errors -------------------------------------------------------------

    def save_error(self, error_json: dict[str, Any]) -> None:
        self._enqueue_write(
            "INSERT INTO errors(task_id, url_key, stage, error_type, attempt, "
            "next_retry_at, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                error_json.get("task_id", ""),
                error_json.get("url_key", ""),
                error_json["stage"],
                error_json["error_type"],
                error_json.get("attempt", 0),
                error_json.get("next_retry_at"),
                error_json.get("created_at", ""),
            ),
        )

    async def get_errors_by_task(self, task_id: str) -> list[dict[str, Any]]:
        cur = await self._execute_now("SELECT * FROM errors WHERE task_id = ? ORDER BY id", (task_id,))
        return [dict(r) for r in await cur.fetchall()]

    async def get_errors_by_url_key(self, url_key: str) -> list[dict[str, Any]]:
        cur = await self._execute_now("SELECT * FROM errors WHERE url_key = ? ORDER BY id", (url_key,))
        return [dict(r) for r in await cur.fetchall()]

    #: robots_cache -------------------------------------------------------

    def save_robots(self, robots_json: dict[str, Any]) -> None:
        self._enqueue_write(
            "INSERT OR REPLACE INTO robots_cache(domain, raw, fetched_at, ttl) VALUES(?, ?, ?, ?)",
            (
                robots_json["domain"],
                robots_json.get("raw", ""),
                robots_json.get("fetched_at", ""),
                robots_json.get("ttl", 86400),
            ),
        )

    async def get_robots(self, domain: str) -> dict[str, Any] | None:
        cur = await self._execute_now("SELECT * FROM robots_cache WHERE domain = ?", (domain,))
        row = await cur.fetchone()
        return dict(row) if row else None


_EMBEDDINGS_DDL = """
CREATE TABLE IF NOT EXISTS embeddings (
    content_hash TEXT PRIMARY KEY,
    model        TEXT DEFAULT '',
    dims         INTEGER DEFAULT 0,
    vector       BLOB,
    created_at   TEXT NOT NULL
);
"""


class SqliteEmbeddingCache:
    """EmbeddingCache backed by a global SQLite file shared across tasks.

    Unlike SqliteStorage (one timestamped DB per crawl run), this cache
    lives at a fixed path (typically ``results/embedding_cache.db``),
    so vectors persist across runs and tasks.  It owns its own
    aiosqlite connection, opened lazily on first use, and commits after
    every put: an ungraceful process exit loses nothing.  Callers that
    manage a long-lived process should call ``close()`` when done.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def _ensure_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self._db_path)
            await self._conn.executescript(_EMBEDDINGS_DDL)
            await self._conn.commit()
        return self._conn

    async def get_vectors(self, content_hashes: list[str]) -> dict[str, list[float]]:
        if not content_hashes:
            return {}
        conn = await self._ensure_conn()
        async with self._lock:
            placeholders = ",".join("?" * len(content_hashes))
            cur = await conn.execute(
                f"SELECT content_hash, vector FROM embeddings WHERE content_hash IN ({placeholders})",  # noqa: S608
                tuple(content_hashes),
            )
            rows = await cur.fetchall()
        out: dict[str, list[float]] = {}
        for row in rows:
            if row[1] is None:
                continue
            arr = array("f")
            arr.frombytes(row[1])
            out[row[0]] = arr.tolist()
        return out

    async def put_vectors(self, entries: list[tuple[str, list[float]]], model: str) -> None:
        if not entries:
            return
        conn = await self._ensure_conn()
        async with self._lock:
            for content_hash, vector in entries:
                await conn.execute(
                    "INSERT OR REPLACE INTO embeddings(content_hash, model, dims, vector, created_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        content_hash,
                        model,
                        len(vector),
                        array("f", vector).tobytes(),
                        datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    ),
                )
            await conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
