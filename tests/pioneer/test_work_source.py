"""PriorityHeapSource: ordering, and the four gate outcomes.

The gate is the contract between the shell and the source, and its four
outcomes are not interchangeable: a deferred item comes back, a dropped
one does not, and a stop ends the scan for everyone. Collapsing any two
would change how a crawl behaves, so each gets its own test.
"""

from __future__ import annotations

import datetime

import pytest

from crawlme.pioneer.work_source import Gate, PriorityHeapSource
from crawlme.schemas import URL, FrontierItem


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _item(key: str, priority: float = 0.5, domain: str = "x.com", **kw) -> FrontierItem:
    url = URL(raw=f"https://{domain}/{key}", canonical=f"https://{domain}/{key}", url_key=key, reg_domain=domain)
    return FrontierItem(url=url, url_key=key, priority=priority, reg_domain=domain, **kw)


def _always(decision: Gate):
    return lambda item, now: decision


@pytest.mark.asyncio
async def test_takes_highest_priority_first():
    src = PriorityHeapSource()
    await src.add([_item("low", 0.1), _item("high", 0.9), _item("mid", 0.5)])
    order = [(await src.take(_now(), _always(Gate.TAKE))).url_key for _ in range(3)]
    assert order == ["high", "mid", "low"]


@pytest.mark.asyncio
async def test_equal_priority_keeps_push_order():
    src = PriorityHeapSource()
    await src.add([_item("first", 0.5), _item("second", 0.5)])
    assert (await src.take(_now(), _always(Gate.TAKE))).url_key == "first"


@pytest.mark.asyncio
async def test_duplicate_keys_are_ignored():
    src = PriorityHeapSource()
    await src.add([_item("k"), _item("k")])
    assert src.size == 1


@pytest.mark.asyncio
async def test_empty_source_yields_nothing():
    assert await PriorityHeapSource().take(_now(), _always(Gate.TAKE)) is None


#: the four gate outcomes -------------------------------------------------


@pytest.mark.asyncio
async def test_defer_keeps_the_item_for_later():
    """Deferred is not dropped: the item still counts and comes back."""
    src = PriorityHeapSource()
    await src.add([_item("k")])
    assert await src.take(_now(), _always(Gate.DEFER)) is None
    assert src.size == 1, "a deferred item vanished from the source"

    # Once the gate relents, the same item is offered again.
    assert (await src.take(_now(), _always(Gate.TAKE))).url_key == "k"


@pytest.mark.asyncio
async def test_drop_discards_the_item_permanently():
    src = PriorityHeapSource()
    await src.add([_item("k")])
    assert await src.take(_now(), _always(Gate.DROP)) is None
    assert src.size == 0, "a dropped item was kept"


@pytest.mark.asyncio
async def test_stop_ends_the_scan_without_consuming():
    """STOP is about the run, not the item: nothing may be discarded."""
    src = PriorityHeapSource()
    await src.add([_item("a"), _item("b")])
    assert await src.take(_now(), _always(Gate.STOP)) is None
    assert src.size == 2


@pytest.mark.asyncio
async def test_scan_skips_a_dropped_item_and_takes_the_next():
    src = PriorityHeapSource()
    await src.add([_item("bad", 0.9), _item("good", 0.1)])
    gate = lambda item, now: Gate.DROP if item.url_key == "bad" else Gate.TAKE  # noqa: E731
    assert (await src.take(_now(), gate)).url_key == "good"


@pytest.mark.asyncio
async def test_drain_recovers_a_parked_item_on_a_later_call():
    """The retry loop exists for items parked by an earlier call.

    Within one call a deferred item stays deferred, otherwise a gate that
    defers for a reason other than the clock would spin forever. Across
    calls, a due item must come back without the caller doing anything.
    """
    src = PriorityHeapSource()
    past = _now() - datetime.timedelta(seconds=1)
    await src.add([_item("k", next_available_at=past)])

    assert await src.take(_now(), _always(Gate.DEFER)) is None
    assert src.size == 1

    assert (await src.take(_now(), _always(Gate.TAKE))).url_key == "k"


@pytest.mark.asyncio
async def test_a_gate_that_always_defers_terminates():
    """Regression: this looped forever before deferrals were held back."""
    src = PriorityHeapSource()
    past = _now() - datetime.timedelta(seconds=1)
    await src.add([_item("a", next_available_at=past), _item("b", next_available_at=past)])
    assert await src.take(_now(), _always(Gate.DEFER)) is None
    assert src.size == 2


#: aging and checkpoints --------------------------------------------------


@pytest.mark.asyncio
async def test_waiting_raises_effective_priority():
    src = PriorityHeapSource(aging_window=10.0, age_factor=1.0)
    await src.add([_item("k", 0.5)])
    taken = await src.take(_now() + datetime.timedelta(seconds=10), _always(Gate.TAKE))
    assert taken.priority > 0.5


@pytest.mark.asyncio
async def test_dump_and_load_round_trip():
    src = PriorityHeapSource()
    await src.add([_item("a", 0.9), _item("b", 0.1)])
    state = src.dump()

    restored = PriorityHeapSource()
    restored.load(state)
    assert restored.size == 2
    assert (await restored.take(_now(), _always(Gate.TAKE))).url_key == "a"


@pytest.mark.asyncio
async def test_discard_removes_a_key_from_the_index():
    src = PriorityHeapSource()
    await src.add([_item("k")])
    src.discard("k")
    assert not src.contains("k")
    assert src.keys() == set()
