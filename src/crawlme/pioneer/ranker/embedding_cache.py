"""SQLite-backed embedding vector cache, owned by the ranking stage.

A global file shared across tasks (results/embedding_cache.db), so
vectors persist model-scoped by content hash and repeated texts skip
the provider entirely.  Lives with the ranker, not in the state
package: the cache exists to serve EmbeddingRanker and is swapped or
dropped together with it.
"""

from __future__ import annotations

import asyncio
import datetime
from array import array
from pathlib import Path

import aiosqlite

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
