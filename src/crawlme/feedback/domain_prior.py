"""DomainPriorStore: the cross-task persistence of the feedback loop.

One global SQLite file (results/feedback.db) accumulates every
analyzed page's contribution per domain, so later tasks start informed
instead of blind.  Lives with the feedback subsystem, not in the state
package: the store exists to serve the loop and is disabled together
with it.

Persistence is best-effort.  Contributions buffer in memory and flush
on close(), so a completed run always lands, a crashed one loses its
tail.  close() is required by the caller contract, since the
connection's aiosqlite worker thread would otherwise keep the
interpreter alive after the crawl.
"""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path

import aiosqlite

#: domain_prior ---------------------------------------------------------
#
# The cross-task per-domain reputation store.  Like SqliteEmbeddingCache
# it lives at a fixed global path (results/feedback.db) shared by every
# task, so the accumulation survives run boundaries.  The feedback
# subsystem owns the semantics (what to record, when to flush); this
# class owns only the persistence mechanics.

_DOMAIN_PRIOR_DDL = """
CREATE TABLE IF NOT EXISTS domain_prior (
    reg_domain       TEXT PRIMARY KEY,
    times_relevant   INTEGER DEFAULT 0,
    times_irrelevant INTEGER DEFAULT 0,
    sum_relevance    REAL DEFAULT 0.0,
    updated_at       TEXT NOT NULL
);
"""


class DomainPriorStore:
    """Cross-task per-domain reputation in one global SQLite file.

    ``record()`` is synchronous and buffers in memory; ``close()``
    writes everything through atomic counter-increment upserts and
    releases the connection.  Both are required by the caller
    contract, since the connection's aiosqlite worker thread would
    otherwise keep the interpreter alive after the crawl.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._pending: list[tuple[str, bool, float]] = []
        self._closed = False

    def record(self, reg_domain: str, *, relevant: bool, relevance_score: float) -> None:
        """Buffer one analyzed page's contribution (no I/O, no await)."""
        if not reg_domain or self._closed:
            return
        self._pending.append((reg_domain, relevant, relevance_score))

    async def load_all(self) -> dict[str, dict[str, float]]:
        """Read every row: reg_domain -> {times_relevant, times_irrelevant, sum_relevance}."""
        conn = await self._ensure_conn()
        async with self._lock:
            cur = await conn.execute(
                "SELECT reg_domain, times_relevant, times_irrelevant, sum_relevance FROM domain_prior"
            )
            rows = await cur.fetchall()
        out: dict[str, dict[str, float]] = {}
        for reg_domain, rel, irrel, total in rows:
            out[reg_domain] = {"times_relevant": rel, "times_irrelevant": irrel, "sum_relevance": total}
        return out

    async def close(self) -> None:
        """Write the buffered records and release the connection (idempotent)."""
        if self._closed:
            return
        self._closed = True
        conn = await self._ensure_conn()
        async with self._lock:
            for reg_domain, relevant, relevance_score in self._pending:
                rel, irrel = (1, 0) if relevant else (0, 1)
                # Counter-increment upsert: concurrent writers sum into
                # the same row instead of clobbering each other.
                await conn.execute(
                    "INSERT INTO domain_prior(reg_domain, times_relevant, times_irrelevant, "
                    "sum_relevance, updated_at) VALUES(?, ?, ?, ?, ?) "
                    "ON CONFLICT(reg_domain) DO UPDATE SET "
                    "times_relevant = times_relevant + excluded.times_relevant, "
                    "times_irrelevant = times_irrelevant + excluded.times_irrelevant, "
                    "sum_relevance = sum_relevance + excluded.sum_relevance, "
                    "updated_at = excluded.updated_at",
                    (reg_domain, rel, irrel, relevance_score, datetime.datetime.now(datetime.timezone.utc).isoformat()),
                )
            await conn.commit()
            self._pending.clear()
            await conn.close()
            self._conn = None

    async def _ensure_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self._db_path)
            await self._conn.executescript(_DOMAIN_PRIOR_DDL)
            await self._conn.commit()
        return self._conn
