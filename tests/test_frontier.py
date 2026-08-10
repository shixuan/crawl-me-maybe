from __future__ import annotations

import datetime

import pytest

from crawlme.pioneer.frontier import Frontier
from crawlme.schemas import URL, FrontierItem


def _item(url_key: str = "k1", priority: float = 0.5, domain: str = "example.com") -> FrontierItem:
    return FrontierItem(
        url=URL(raw=f"https://{domain}/page", canonical=f"https://{domain}/page", url_key=url_key, reg_domain=domain),
        url_key=url_key,
        priority=priority,
        reg_domain=domain,
    )


@pytest.fixture
def frontier() -> Frontier:
    return Frontier()


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
    f = Frontier(domain_budget=1)
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

    f2 = Frontier()
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
    assert len(snap.pending) == 1
    assert snap.pending[0].url_key == "k1"

    f2 = Frontier()
    f2.restore(snap)
    assert f2.size == 1


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
