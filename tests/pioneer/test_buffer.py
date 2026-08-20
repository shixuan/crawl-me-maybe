from __future__ import annotations

import asyncio

import pytest

from crawlme.pioneer.buffer import RoundRobinBuffer
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
def buf() -> RoundRobinBuffer:
    return RoundRobinBuffer(capacity=10)


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
    small = RoundRobinBuffer(capacity=2)
    # Add 2 good candidates (shallow depth, early position).
    await small.add([_candidate("k1", depth=0, position=1), _candidate("k2", depth=0, position=2)])
    assert small.size == 2
    # Adding a better candidate should evict k2 (deeper position).
    await small.add([_candidate("k3", depth=0, position=1)])
    assert small.size == 2
    assert any(c.url.url_key == "k3" for c in await small.drain())


@pytest.mark.asyncio
async def test_eviction_keeps_better_quality(buf):
    small = RoundRobinBuffer(capacity=2)
    await small.add([_candidate("deep", depth=5, position=50)])
    await small.add([_candidate("good", depth=0, position=1)])
    # Buffer full; adding another good candidate should evict "deep".
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
    big = RoundRobinBuffer(capacity=200)
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
    buf = RoundRobinBuffer(capacity=200)

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


#: which candidates get scored at all --------------------------------------


def _seeded(key: str, seed: str) -> Candidate:
    url = f"https://example.com/{key}"
    return Candidate(url=URL(raw=url, canonical=url, url_key=key), seed_url_key=seed)


@pytest.mark.asyncio
async def test_a_drain_takes_a_turn_from_each_seed():
    """This is the gate that binds: what leaves here is what gets scored.

    First-come-first-served let one account's listing fill the queue, and
    a run over five accounts read fifty-three posts from that one and
    none at all from three others.
    """
    buf = RoundRobinBuffer()
    await buf.add([_seeded(f"loud{i}", "a") for i in range(10)])
    await buf.add([_seeded("quiet_b", "b"), _seeded("quiet_c", "c")])

    got = [c.url.url_key for c in await buf.drain(4)]
    assert "quiet_b" in got and "quiet_c" in got
    assert sum(1 for k in got if k.startswith("loud")) == 2


@pytest.mark.asyncio
async def test_within_one_seed_the_oldest_goes_first():
    """None of them are scored yet, so there is nothing else to prefer."""
    buf = RoundRobinBuffer()
    await buf.add([_seeded("first", "a"), _seeded("second", "a")])
    assert [c.url.url_key for c in await buf.drain(2)] == ["first", "second"]


@pytest.mark.asyncio
async def test_a_seed_that_runs_out_gives_its_turns_away():
    buf = RoundRobinBuffer()
    await buf.add([_seeded("only_a", "a")])
    await buf.add([_seeded(f"b{i}", "b") for i in range(3)])
    assert sorted(c.url.url_key for c in await buf.drain(4)) == ["b0", "b1", "b2", "only_a"]


@pytest.mark.asyncio
async def test_draining_everything_needs_no_turns():
    buf = RoundRobinBuffer()
    await buf.add([_seeded("a1", "a"), _seeded("b1", "b")])
    assert len(await buf.drain()) == 2
    assert buf.is_empty


@pytest.mark.asyncio
async def test_candidates_without_a_seed_share_one_turn():
    """A link graph before seeds are threaded through still behaves."""
    buf = RoundRobinBuffer()
    await buf.add(
        [Candidate(url=URL(raw=f"https://x/{i}", canonical=f"https://x/{i}", url_key=str(i))) for i in range(4)]
    )
    assert len(await buf.drain(2)) == 2


@pytest.mark.asyncio
async def test_more_seeds_than_a_batch_still_all_get_turns():
    """A batch is often smaller than the seed list.

    Restarting the rotation at the front each time would let the first
    `n` seeds take every turn and leave the rest exactly as starved as
    first-come-first-served did, only with a larger cartel.
    """
    buf = RoundRobinBuffer()
    await buf.add([_seeded(f"s{s}_{i}", f"seed{s}") for s in range(50) for i in range(4)])

    seen: dict[str, int] = {}
    for _ in range(5):
        for c in await buf.drain(20):
            seen[c.seed_url_key] = seen.get(c.seed_url_key, 0) + 1

    assert len(seen) == 50, "every seed was reached"
    assert set(seen.values()) == {2}, "and reached equally"


@pytest.mark.asyncio
async def test_the_rotation_resumes_where_it_stopped():
    buf = RoundRobinBuffer()
    await buf.add([_seeded(f"{s}1", s) for s in ("a", "b", "c")])
    await buf.add([_seeded(f"{s}2", s) for s in ("a", "b", "c")])

    first = [c.seed_url_key for c in await buf.drain(2)]
    second = [c.seed_url_key for c in await buf.drain(2)]
    assert first == ["a", "b"]
    assert second[0] == "c", "the next drain picks up at the seed that was skipped"
