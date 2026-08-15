"""Tests for SqliteEmbeddingCache, owned by the ranking stage."""

from __future__ import annotations

import asyncio
import time

import pytest

from crawlme.pioneer.ranker.embedding_cache import SqliteEmbeddingCache


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def embedding_cache(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    cache = SqliteEmbeddingCache(tmp_path / "embedding_cache.db")
    yield cache
    loop.run_until_complete(cache.close())
    loop.close()


def test_embedding_roundtrip(embedding_cache):
    """Vector survives the float32 BLOB roundtrip."""
    vec = [0.1, 0.2, 0.3, -0.4]
    _run(embedding_cache.put_vectors([("h1", vec)], "local/model-x"))

    got = _run(embedding_cache.get_vectors(["h1"]))
    assert "h1" in got
    assert len(got["h1"]) == 4
    assert got["h1"] == pytest.approx(vec, abs=1e-6)


def test_embedding_missing_hashes(embedding_cache):
    got = _run(embedding_cache.get_vectors(["nope1", "nope2"]))
    assert got == {}


def test_embedding_cache_close_stops_worker_thread(embedding_cache):
    """The cache's aiosqlite worker thread must exit on close.  It is a
    non-daemon thread, so a leak keeps the interpreter hanging at
    process exit long after the crawl is COMPLETED."""
    _run(embedding_cache.put_vectors([("h1", [0.5])], "local/a"))
    assert embedding_cache._conn is not None
    thread = embedding_cache._conn._thread
    assert thread.is_alive()
    _run(embedding_cache.close())
    # The close future resolves just before the worker exits its loop.
    for _ in range(100):
        if not thread.is_alive():
            break
        time.sleep(0.01)
    assert not thread.is_alive()


def test_embedding_empty_list(embedding_cache):
    assert _run(embedding_cache.get_vectors([])) == {}
    assert _run(embedding_cache.put_vectors([], "local/a")) is None


def test_embedding_last_write_wins_on_same_hash(embedding_cache):
    """Same content hash key: later write overwrites.  Model scoping is
    the caller's job (hash includes the model name)."""
    _run(embedding_cache.put_vectors([("h-same", [1.0, 0.0])], "local/a"))
    _run(embedding_cache.put_vectors([("h-same", [0.0, 1.0])], "local/b"))
    got = _run(embedding_cache.get_vectors(["h-same"]))
    assert got["h-same"] == pytest.approx([0.0, 1.0])


def test_embedding_multiple_vectors(embedding_cache):
    _run(embedding_cache.put_vectors([("h1", [1.0]), ("h2", [2.0]), ("h3", [3.0])], "local/a"))
    got = _run(embedding_cache.get_vectors(["h1", "h3", "missing"]))
    assert set(got.keys()) == {"h1", "h3"}
    assert got["h1"] == pytest.approx([1.0])


def test_embedding_persists_across_reopen(tmp_path):
    """The whole point: cache data survives connection close and reopen."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    path = tmp_path / "embedding_cache.db"
    cache1 = SqliteEmbeddingCache(path)
    loop.run_until_complete(cache1.put_vectors([("h1", [7.0])], "local/a"))
    loop.run_until_complete(cache1.close())

    cache2 = SqliteEmbeddingCache(path)
    got = loop.run_until_complete(cache2.get_vectors(["h1"]))
    loop.run_until_complete(cache2.close())
    loop.close()
    assert got["h1"] == pytest.approx([7.0])
