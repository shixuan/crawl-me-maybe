from __future__ import annotations

import asyncio

import pytest

from crawlme.pioneer.buffer import CandidateBuffer
from crawlme.schemas import URL, Candidate


def _candidate(url_key: str = "k1", raw: str = "https://example.com", **kw) -> Candidate:
    defaults: dict = dict(
        url=URL(raw=raw, canonical=raw, url_key=url_key, reg_domain="example.com"),
        depth=0,
        position=1,
    )
    defaults.update(kw)
    return Candidate(**defaults)


def _batch(n: int) -> list[Candidate]:
    return [_candidate(f"k{i}") for i in range(n)]


@pytest.fixture
def buf() -> CandidateBuffer:
    return CandidateBuffer(capacity=10)


# -- add ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_batch(buf):
    await buf.add(_batch(5))
    assert buf.size == 5


@pytest.mark.asyncio
async def test_dedup_skips_seen_url_key(buf):
    await buf.add([_candidate("k1")])
    await buf.add([_candidate("k1")])
    assert buf.size == 1


@pytest.mark.asyncio
async def test_eviction_when_full(buf):
    small = CandidateBuffer(capacity=2)
    # Add 2 good candidates (shallow depth, early position).
    await small.add([_candidate("k1", depth=0, position=1), _candidate("k2", depth=0, position=2)])
    assert small.size == 2
    # Add a better candidate — should evict k2 (deeper position).
    await small.add([_candidate("k3", depth=0, position=1)])
    assert small.size == 2
    assert any(c.url.url_key == "k3" for c in await small.drain())


@pytest.mark.asyncio
async def test_eviction_keeps_better_quality(buf):
    small = CandidateBuffer(capacity=2)
    await small.add([_candidate("deep", depth=5, position=50)])
    await small.add([_candidate("good", depth=0, position=1)])
    # Buffer full; add another good candidate — should evict "deep".
    await small.add([_candidate("good2", depth=0, position=2)])
    batch = await small.drain()
    keys = {c.url.url_key for c in batch}
    assert "good" in keys
    assert "good2" in keys
    assert "deep" not in keys


@pytest.mark.asyncio
async def test_add_sets_buffered_status(buf):
    await buf.add([_candidate("k1")])
    batch = await buf.drain()
    assert batch[0].status == "BUFFERED"


# -- drain --------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_returns_all_by_default(buf):
    await buf.add(_batch(5))
    batch = await buf.drain()
    assert len(batch) == 5
    assert buf.is_empty


@pytest.mark.asyncio
async def test_drain_n_limited(buf):
    await buf.add(_batch(5))
    batch = await buf.drain(n=2)
    assert len(batch) == 2
    assert buf.size == 3


@pytest.mark.asyncio
async def test_drain_preserves_seen(buf):
    await buf.add([_candidate("k1")])
    await buf.drain()
    await buf.add([_candidate("k1")])
    assert buf.size == 0  # still rejected


# -- ready --------------------------------------------------------------


@pytest.mark.asyncio
async def test_ready_when_size_reaches_100(buf):
    big = CandidateBuffer(capacity=200)
    await big.add([_candidate(f"k{i}") for i in range(100)])
    assert big.ready()


@pytest.mark.asyncio
async def test_not_ready_when_small_and_fresh(buf):
    assert not buf.ready()
    await buf.add(_batch(5))
    assert not buf.ready()  # <100 and just added


@pytest.mark.asyncio
async def test_ready_when_frontier_hungry(buf):
    await buf.add([_candidate("k1")])
    assert buf.ready(frontier_hungry=True)


# -- wait_until ---------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_until_wakes_on_add():
    buf = CandidateBuffer(capacity=200)

    async def delayed_add():
        await asyncio.sleep(0.05)
        await buf.add([_candidate(f"k{i}") for i in range(100)])

    async def waiter():
        await buf.wait_until()
        return buf.ready()

    # Start waiter first, then add to trigger it.
    result, _ = await asyncio.gather(waiter(), delayed_add())
    assert result is True


# -- properties ---------------------------------------------------------


def test_is_empty(buf):
    assert buf.is_empty


@pytest.mark.asyncio
async def test_seen_count(buf):
    await buf.add(_batch(3))
    assert buf.seen_count == 3
    await buf.drain()
    assert buf.seen_count == 3  # preserved
