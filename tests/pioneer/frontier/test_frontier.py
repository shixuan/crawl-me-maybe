from __future__ import annotations

import datetime
import json

import pytest

from crawlme.pioneer.frontier import GatedFrontier, PriorityQueue
from crawlme.schemas import URL, FrontierItem, FrontierSnapshot


def _item(url_key: str = "k1", priority: float = 0.5, domain: str = "example.com") -> FrontierItem:
    return FrontierItem(
        url=URL(raw=f"https://{domain}/page", canonical=f"https://{domain}/page", url_key=url_key, reg_domain=domain),
        url_key=url_key,
        priority=priority,
        reg_domain=domain,
    )


@pytest.fixture
def frontier() -> GatedFrontier:
    return GatedFrontier()


@pytest.mark.asyncio
async def test_push_and_pop_by_priority(frontier):
    items = [_item("k1", 0.1), _item("k2", 0.9), _item("k3", 0.5)]
    await frontier.push_batch(items)
    popped = await frontier.pop_next()
    assert popped is not None
    assert popped.url_key == "k2"


@pytest.mark.asyncio
async def test_pop_respects_domain_gate(frontier):
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)

    item = _item("k1", 1.0)
    item.next_available_at = future
    await frontier.push_batch([item])

    popped = await frontier.pop_next()
    assert popped is None


@pytest.mark.asyncio
async def test_domain_budget_exhausted(frontier):
    f = GatedFrontier(domain_budget=1)
    items = [_item("k1", 0.5, "x.com"), _item("k2", 0.9, "x.com")]
    await f.push_batch(items)

    first = await f.pop_next()
    assert first is not None
    await f.record_outcome(first, "COMPLETED")

    second = await f.pop_next()
    assert second is None


@pytest.mark.asyncio
async def test_global_budget(frontier):
    items = [_item("k1", 0.3), _item("k2", 0.7)]
    await frontier.push_batch(items)

    first = await frontier.pop_next(global_budget=1)
    assert first is not None
    await frontier.record_outcome(first, "COMPLETED")

    second = await frontier.pop_next(global_budget=1)
    assert second is None


@pytest.mark.asyncio
async def test_record_outcome_updates_visited(frontier):
    item = _item("k1")
    await frontier.push_batch([item])
    popped = await frontier.pop_next()
    await frontier.record_outcome(popped, "COMPLETED")
    assert frontier.contains("k1")


@pytest.mark.asyncio
async def test_duplicate_not_pushed(frontier):
    await frontier.push_batch([_item("k1")])
    await frontier.push_batch([_item("k1")])
    assert frontier.size == 1


@pytest.mark.asyncio
async def test_snapshot_roundtrip(frontier):
    items = [_item("k1", 0.5, "x.com"), _item("k2", 0.8, "y.com")]
    await frontier.push_batch(items)

    popped = await frontier.pop_next()
    await frontier.record_outcome(popped, "COMPLETED")

    snap = frontier.snapshot(task_id="t1")
    assert len(snap.visited) == 1

    f2 = GatedFrontier()
    f2.restore(snap)
    assert f2.contains("k1")
    assert f2.contains("k2")
    assert f2.size == 1


@pytest.mark.asyncio
async def test_snapshot_pending_gated_items(frontier):
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    item = _item("k1", 1.0)
    item.next_available_at = future
    await frontier.push_batch([item])
    await frontier.pop_next()

    snap = frontier.snapshot()
    f2 = GatedFrontier()
    f2.restore(snap)
    assert f2.size == 1, "an item waiting on a cooldown survived the round trip"
    assert f2.get_prefilter_context().is_visited_or_queued("k1")


@pytest.mark.asyncio
async def test_pending_items_retry_after_gate(frontier):
    now = datetime.datetime.now(datetime.timezone.utc)
    future = now + datetime.timedelta(hours=1)
    item = _item("k1", 1.0)
    item.next_available_at = future
    await frontier.push_batch([item])

    popped = await frontier.pop_next(now=now)
    assert popped is None

    popped = await frontier.pop_next(now=future + datetime.timedelta(seconds=1))
    assert popped is not None
    assert popped.url_key == "k1"


@pytest.mark.asyncio
async def test_a_page_being_fetched_is_not_queued_again():
    """It is neither waiting nor finished, and dedup has to cover both.

    Discovered again from another page mid-fetch, it would otherwise be
    read twice and analysed twice.
    """
    f = GatedFrontier(domain_budget=0, source=PriorityQueue())
    await f.push_batch([_item("dup", priority=0.9)])
    taken = await f.pop_next(now=datetime.datetime.now(datetime.timezone.utc))
    assert taken is not None

    await f.push_batch([_item("dup", priority=0.9)])
    assert f.size == 0, "the same page came back while it was in flight"
    assert f.get_prefilter_context().is_visited_or_queued("dup")


@pytest.mark.asyncio
async def test_a_checkpoint_keeps_the_queue():
    """The snapshot used to copy one ordering's internals by name.

    With `heap` absent from a composed ordering's state, every
    checkpoint stored an empty queue and every resume began with nothing
    to fetch, without an error anywhere.
    """
    make = PriorityQueue
    f = GatedFrontier(source=make())
    await f.push_batch([_item("a", 0.9), _item("b", 0.5)])
    assert f.size == 2

    restored = GatedFrontier(source=make())
    restored.restore(f.snapshot())
    assert restored.size == 2
    assert restored.get_prefilter_context().is_visited_or_queued("a")


@pytest.mark.asyncio
async def test_a_checkpoint_written_before_orderings_carried_state_still_loads():
    f = GatedFrontier()
    await f.push_batch([_item("old", 0.7)])
    snap = f.snapshot()
    legacy = snap.model_copy(update={"ordering": {}, "heap": [_item("old", 0.7)]})

    restored = GatedFrontier()
    restored.restore(legacy)
    assert restored.size == 1


@pytest.mark.asyncio
async def test_a_checkpoint_survives_the_trip_through_json():
    """The path a real resume takes, which no test walked before.

    In memory dump() and load() agreed because both spoke models; on
    disk the state is data, and the first thing load() did with it was
    read an attribute.
    """
    make = PriorityQueue
    f = GatedFrontier(source=make())
    await f.push_batch([_item("a", 0.9), _item("b", 0.5)])

    on_disk = json.loads(f.snapshot().model_dump_json())
    restored = GatedFrontier(source=make())
    restored.restore(FrontierSnapshot.model_validate(on_disk))

    assert restored.size == 2
    got = await restored.pop_next(now=datetime.datetime.now(datetime.timezone.utc))
    assert got is not None and got.url_key == "a", "priority survived too"
