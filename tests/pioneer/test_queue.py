"""PriorityQueue: ordering, and the four gate outcomes.

The gate is the contract between the shell and the source, and its four
outcomes are not interchangeable: a deferred item comes back, a dropped
one does not, and a stop ends the scan for everyone. Collapsing any two
would change how a crawl behaves, so each gets its own test.
"""

from __future__ import annotations

import datetime

import pytest

from crawlme.pioneer.queue import Gate, PriorityQueue
from crawlme.schemas import URL, FrontierItem


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _item(key: str, priority: float = 0.5, domain: str = "x.com", **kw) -> FrontierItem:
    url = URL(raw=f"https://{domain}/{key}", canonical=f"https://{domain}/{key}", url_key=key, reg_domain=domain)
    return FrontierItem(url=url, url_key=key, priority=priority, reg_domain=domain, **kw)


def _always(decision: Gate):
    return lambda item, now: decision


@pytest.mark.asyncio
async def test_highest_first():
    src = PriorityQueue()
    await src.add([_item("low", 0.1), _item("high", 0.9), _item("mid", 0.5)])
    order = [(await src.take(_now(), _always(Gate.TAKE))).url_key for _ in range(3)]
    assert order == ["high", "mid", "low"]


@pytest.mark.asyncio
async def test_tie_push_order():
    src = PriorityQueue()
    await src.add([_item("first", 0.5), _item("second", 0.5)])
    assert (await src.take(_now(), _always(Gate.TAKE))).url_key == "first"


@pytest.mark.asyncio
async def test_dup_ignored():
    src = PriorityQueue()
    await src.add([_item("k"), _item("k")])
    assert src.size == 1


@pytest.mark.asyncio
async def test_empty_yields():
    assert await PriorityQueue().take(_now(), _always(Gate.TAKE)) is None


# the four gate outcomes -------------------------------------------------


@pytest.mark.asyncio
async def test_defer_keeps():
    """Deferred is not dropped: the item still counts and comes back."""
    src = PriorityQueue()
    await src.add([_item("k")])
    assert await src.take(_now(), _always(Gate.DEFER)) is None
    assert src.size == 1, "a deferred item vanished from the source"

    # Once the gate relents, the same item is offered again.
    assert (await src.take(_now(), _always(Gate.TAKE))).url_key == "k"


@pytest.mark.asyncio
async def test_drop_discards():
    src = PriorityQueue()
    await src.add([_item("k")])
    assert await src.take(_now(), _always(Gate.DROP)) is None
    assert src.size == 0, "a dropped item was kept"


@pytest.mark.asyncio
async def test_stop_scan():
    """STOP is about the run, not the item: nothing may be discarded."""
    src = PriorityQueue()
    await src.add([_item("a"), _item("b")])
    assert await src.take(_now(), _always(Gate.STOP)) is None
    assert src.size == 2


@pytest.mark.asyncio
async def test_scan_skips():
    src = PriorityQueue()
    await src.add([_item("bad", 0.9), _item("good", 0.1)])
    gate = lambda item, now: Gate.DROP if item.url_key == "bad" else Gate.TAKE  # noqa: E731
    assert (await src.take(_now(), gate)).url_key == "good"


@pytest.mark.asyncio
async def test_drain_parked():
    """The retry loop exists for items parked by an earlier call.

    Within one call a deferred item stays deferred, otherwise a gate that
    defers for a reason other than the clock would spin forever. Across
    calls, a due item must come back without the caller doing anything.
    """
    src = PriorityQueue()
    past = _now() - datetime.timedelta(seconds=1)
    await src.add([_item("k", next_available_at=past)])

    assert await src.take(_now(), _always(Gate.DEFER)) is None
    assert src.size == 1

    assert (await src.take(_now(), _always(Gate.TAKE))).url_key == "k"


@pytest.mark.asyncio
async def test_defer_ends():
    """Regression: this looped forever before deferrals were held back."""
    src = PriorityQueue()
    past = _now() - datetime.timedelta(seconds=1)
    await src.add([_item("a", next_available_at=past), _item("b", next_available_at=past)])
    assert await src.take(_now(), _always(Gate.DEFER)) is None
    assert src.size == 2


# aging and checkpoints --------------------------------------------------


@pytest.mark.asyncio
async def test_waiting_ages():
    src = PriorityQueue(aging_window=10.0, age_factor=1.0)
    await src.add([_item("k", 0.5)])
    taken = await src.take(_now() + datetime.timedelta(seconds=10), _always(Gate.TAKE))
    assert taken.priority > 0.5


@pytest.mark.asyncio
async def test_dump_load():
    src = PriorityQueue()
    await src.add([_item("a", 0.9), _item("b", 0.1)])
    state = src.dump()

    restored = PriorityQueue()
    restored.load(state)
    assert restored.size == 2
    assert (await restored.take(_now(), _always(Gate.TAKE))).url_key == "a"


@pytest.mark.asyncio
async def test_discard_key():
    src = PriorityQueue()
    await src.add([_item("k")])
    src.discard("k")
    assert not src.contains("k")
    assert src.keys() == set()


# what size means --------------------------------------------------------


@pytest.mark.asyncio
async def test_discard_count():
    """A heap cannot delete from the middle, so the entry stays behind.

    Counting it keeps an emptied frontier looking busy, and the run then
    never reaches the check that would have ended it.
    """
    src = PriorityQueue()
    await src.add([_item("a"), _item("b")])
    src.discard("a")
    assert src.size == 1


@pytest.mark.asyncio
async def test_inflight_unwait():
    src = PriorityQueue()
    await src.add([_item("a"), _item("b")])
    await src.take(_now(), _always(Gate.TAKE))
    assert src.size == 1, "the item handed out is no longer queued"


@pytest.mark.asyncio
async def test_count_discard():
    """The tombstone is cleared where it is found, and only once."""
    src = PriorityQueue()
    await src.add([_item("a", priority=0.9), _item("b", priority=0.1)])
    src.discard("a")
    taken = await src.take(_now(), _always(Gate.TAKE))
    assert taken is not None and taken.url_key == "b"
    assert src.size == 0


# what "already have it" means -------------------------------------------


@pytest.mark.asyncio
async def test_inflight_held():
    """Dedup asks contains() whether a URL is already spoken for.

    Forgetting an item the moment it is handed out lets the same page be
    discovered again mid-fetch and queued a second time.
    """
    src = PriorityQueue()
    await src.add([_item("a")])
    await src.take(_now(), _always(Gate.TAKE))
    assert src.contains("a") and "a" in src.keys()
    assert src.size == 0, "held, but no longer waiting"


@pytest.mark.asyncio
async def test_discard_cooling():
    src = PriorityQueue()
    await src.add([_item("a")])
    await src.take(_now(), _always(Gate.DEFER))
    assert src.size == 1, "waiting on a cooldown, still work"
    src.discard("a")
    assert src.size == 0


@pytest.mark.asyncio
async def test_no_negative():
    """A negative size is not a cosmetic error.

    The rank pump wakes on "the frontier is empty", written as size == 0.
    At -1 that is never true, so ranked work sits in the buffer and the
    run stalls with no error anywhere. The caller also sets status before
    settling, so nothing here may infer state from it.
    """
    src = PriorityQueue()
    await src.add([_item("a")])
    taken = await src.take(_now(), _always(Gate.TAKE))
    assert taken is not None
    taken.status = "COMPLETED"  # what record_outcome does before discarding
    src.discard(taken.url_key)
    assert src.size == 0


@pytest.mark.asyncio
async def test_size_waiting():
    src = PriorityQueue()
    await src.add([_item("a"), _item("b"), _item("c")])
    await src.take(_now(), _always(Gate.TAKE))
    assert src.size == 2, "one is in flight, two still queued"
