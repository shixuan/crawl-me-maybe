"""Fair ordering across the pages candidates came from.

Best-first answers "what are the best results". Monitoring several
accounts asks a different question, and a run that read fifty-five posts
from one shop and none from the other four answered the first question
correctly and the second not at all.
"""

from __future__ import annotations

import datetime

import pytest

from crawlme.pioneer.work_source import Gate, PriorityHeapSource, RoundRobinSource
from crawlme.schemas import URL, FrontierItem

pytestmark = pytest.mark.asyncio

_NOW = datetime.datetime(2026, 8, 19, tzinfo=datetime.timezone.utc)


def _take_all(gate=lambda item, now: Gate.TAKE):
    return gate


def _item(key: str, source: str, priority: float) -> FrontierItem:
    url = f"https://example.com/{key}"
    return FrontierItem(
        url=URL(raw=url, canonical=url, url_key=key),
        url_key=key,
        priority=priority,
        source_url_key=source,
    )


def _source() -> RoundRobinSource:
    return RoundRobinSource(PriorityHeapSource)


async def _drain(src: RoundRobinSource, n: int) -> list[str]:
    out = []
    for _ in range(n):
        item = await src.take(_NOW, _take_all())
        if item is None:
            break
        src.discard(item.url_key)
        out.append(item.url_key)
    return out


async def test_a_dominant_source_cannot_take_the_whole_budget():
    """The case this exists for, with the shape a real run had."""
    src = _source()
    await src.add([_item(f"loud{i}", "aggregator", 1.0) for i in range(10)])
    await src.add([_item("shop_a", "a", 0.2), _item("shop_b", "b", 0.2)])

    first_four = await _drain(src, 4)
    assert "shop_a" in first_four and "shop_b" in first_four
    assert sum(1 for k in first_four if k.startswith("loud")) == 2


async def test_within_one_source_the_best_still_comes_first():
    src = _source()
    await src.add([_item("weak", "a", 0.1), _item("strong", "a", 0.9)])
    assert await _drain(src, 2) == ["strong", "weak"]


async def test_a_source_that_runs_out_yields_its_turns():
    """A reserved share would have been spent on nothing."""
    src = _source()
    await src.add([_item("only", "a", 0.5)])
    await src.add([_item(f"b{i}", "b", 0.5) for i in range(3)])
    assert sorted(await _drain(src, 4)) == ["b0", "b1", "b2", "only"]


async def test_one_source_behaves_like_the_source_it_wraps():
    src = _source()
    await src.add([_item("c", "a", 0.3), _item("a", "a", 0.9), _item("b", "a", 0.6)])
    assert await _drain(src, 3) == ["a", "b", "c"]


async def test_a_seed_is_its_own_group():
    """It must not queue behind the pages it will go on to find."""
    src = _source()
    await src.add([_item("seed", "", 1.0)])
    await src.add([_item(f"child{i}", "other", 1.0) for i in range(5)])
    assert "seed" in await _drain(src, 2)


async def test_nothing_left_returns_nothing():
    assert await _source().take(_NOW, _take_all()) is None


async def test_a_gate_that_refuses_everything_terminates():
    src = _source()
    await src.add([_item("x", "a", 0.5), _item("y", "b", 0.5)])
    assert await src.take(_NOW, lambda item, now: Gate.DROP) is None


async def test_state_survives_a_round_trip():
    src = _source()
    await src.add([_item("a1", "a", 0.9), _item("b1", "b", 0.5)])
    restored = _source()
    restored.load(src.dump())
    assert restored.size == 2
    assert restored.contains("a1") and restored.contains("b1")
