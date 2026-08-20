"""Two orderings composed: one for the groups, one inside each group.

HybridOrdering implements no ordering itself. It exists so an algorithm
written once can serve either level, and so global best-first stops being
a second code path.
"""

from __future__ import annotations

import datetime

import pytest

from crawlme.pioneer.ordering import BestFirst, Gate, HybridOrdering, Ordering, RoundRobin
from crawlme.schemas import URL, FrontierItem

pytestmark = pytest.mark.asyncio

_NOW = datetime.datetime(2026, 8, 19, tzinfo=datetime.timezone.utc)


def _take(_item, _now):
    return Gate.TAKE


def _item(key: str, seed: str, priority: float) -> FrontierItem:
    url = f"https://example.com/{key}"
    return FrontierItem(
        url=URL(raw=url, canonical=url, url_key=key),
        url_key=key,
        priority=priority,
        seed_url_key=seed,
    )


def _hybrid(outer: Ordering | None = None) -> HybridOrdering:
    return HybridOrdering(lambda i: i.seed_url_key or i.url_key, outer or RoundRobin(), BestFirst)


async def _drain(o: HybridOrdering, n: int, gate=_take) -> list[str]:
    out = []
    for _ in range(n):
        item = await o.take(_NOW, gate)
        if item is None:
            break
        out.append(item.url_key)
    return out


#: fair --------------------------------------------------------------------


async def test_a_loud_seed_cannot_take_the_whole_budget():
    """The run this exists for: one account's posts outranked every
    other account's, so it got fifty-five pages and they got none."""
    o = _hybrid()
    await o.add([_item(f"loud{i}", "aggregator", 1.0) for i in range(10)])
    await o.add([_item("shop_a", "a", 0.2), _item("shop_b", "b", 0.2)])

    first_four = await _drain(o, 4)
    assert "shop_a" in first_four and "shop_b" in first_four
    assert sum(1 for k in first_four if k.startswith("loud")) == 2


async def test_inside_a_seed_the_best_still_comes_first():
    o = _hybrid()
    await o.add([_item("weak", "a", 0.1), _item("strong", "a", 0.9)])
    assert await _drain(o, 2) == ["strong", "weak"]


async def test_a_seed_that_runs_out_yields_its_turns():
    """A reserved share would have been spent on nothing."""
    o = _hybrid()
    await o.add([_item("only", "a", 0.5)])
    await o.add([_item(f"b{i}", "b", 0.5) for i in range(3)])
    assert sorted(await _drain(o, 4)) == ["b0", "b1", "b2", "only"]


#: best --------------------------------------------------------------------


async def test_best_first_outside_is_best_first_everywhere():
    """Not a separate path: the best group's best item is the best item."""
    o = _hybrid(BestFirst())
    await o.add([_item("a_low", "a", 0.1), _item("a_high", "a", 0.9)])
    await o.add([_item("b_mid", "b", 0.5), _item("b_top", "b", 0.95)])
    assert await _drain(o, 4) == ["b_top", "a_high", "b_mid", "a_low"]


async def test_one_seed_behaves_the_same_under_either_plug():
    items = [_item("c", "a", 0.3), _item("a", "a", 0.9), _item("b", "a", 0.6)]
    fair, best = _hybrid(), _hybrid(BestFirst())
    await fair.add(list(items))
    await best.add([_item(i.url_key, "a", i.priority) for i in items])
    assert await _drain(fair, 3) == await _drain(best, 3) == ["a", "b", "c"]


#: the rest ----------------------------------------------------------------


async def test_a_seed_is_its_own_group():
    """It must not queue behind the pages it will go on to find."""
    o = _hybrid()
    await o.add([_item("seed", "", 1.0)])
    await o.add([_item(f"child{i}", "other", 1.0) for i in range(5)])
    assert "seed" in await _drain(o, 2)


async def test_nothing_left_returns_nothing():
    assert await _hybrid().take(_NOW, _take) is None


async def test_a_gate_that_refuses_everything_terminates():
    o = _hybrid()
    await o.add([_item("x", "a", 0.5), _item("y", "b", 0.5)])
    assert await o.take(_NOW, lambda i, n: Gate.DROP) is None


async def test_a_deferred_seed_keeps_its_place():
    """A source waiting on a rate limit resumes its turn, not loses it."""
    o = _hybrid()
    await o.add([_item("slow", "a", 0.9), _item("ready", "b", 0.1)])
    gate = lambda i, n: Gate.DEFER if i.url_key == "slow" else Gate.TAKE  # noqa: E731
    assert await _drain(o, 2, gate) == ["ready"]
    assert o.contains("slow"), "held, not discarded"


async def test_size_and_membership_come_from_the_groups():
    o = _hybrid()
    await o.add([_item("a1", "a", 0.9), _item("b1", "b", 0.5)])
    assert o.size == 2 and o.contains("a1") and not o.contains("nope")
    o.discard("a1")
    assert o.size == 1 and not o.contains("a1")


async def test_state_survives_a_round_trip():
    o = _hybrid()
    await o.add([_item("a1", "a", 0.9), _item("b1", "b", 0.5)])
    restored = _hybrid()
    restored.load(o.dump())
    assert restored.size == 2
    assert restored.contains("a1") and restored.contains("b1")


async def test_the_two_levels_agree_on_what_is_held():
    """Anything the inner ordering remembers, this must remember too.

    It reports to the same dedup check, so a disagreement is a URL
    fetched twice.
    """
    o = _hybrid()
    await o.add([_item("a", "s", 0.5)])
    await o.take(_NOW, _take)
    assert o.contains("a") and "a" in o.keys()
    o.discard("a")
    assert not o.contains("a")
