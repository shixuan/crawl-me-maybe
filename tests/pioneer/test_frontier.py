from __future__ import annotations

import asyncio
import datetime
import json
from types import SimpleNamespace

import pytest

from crawlme.pioneer import Candidate, Frontier, FrontierItem, FrontierSnapshot
from crawlme.pioneer.queue import PriorityQueue
from crawlme.urls import URL
from crawlme.util import subscribe


def _item(
    url_key: str = "k1", priority: float = 0.5, domain: str = "example.com", seed_url_key: str = ""
) -> FrontierItem:
    return FrontierItem(
        url=URL(raw=f"https://{domain}/page", canonical=f"https://{domain}/page", url_key=url_key, reg_domain=domain),
        url_key=url_key,
        priority=priority,
        reg_domain=domain,
        seed_url_key=seed_url_key,
    )


def _candidate(url_key: str, seed_url_key: str = "") -> Candidate:
    raw = f"https://example.com/{url_key}"
    return Candidate(
        url=URL(raw=raw, canonical=raw, url_key=url_key, reg_domain="example.com"),
        seed_url_key=seed_url_key,
    )


@pytest.fixture
def frontier() -> Frontier:
    return Frontier()


@pytest.mark.asyncio
async def test_pop_gated(frontier):
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)

    item = _item("k1", 1.0)
    item.next_available_at = future
    await frontier.push_batch([item])

    popped = await frontier.pop_next()
    assert popped is None


@pytest.mark.asyncio
async def test_domain_spent(frontier):
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
async def test_dup_not_pushed(frontier):
    await frontier.push_batch([_item("k1")])
    await frontier.push_batch([_item("k1")])
    assert frontier.size == 1


@pytest.mark.asyncio
async def test_snapshot_roundtrip(frontier):
    items = [_item("k1", 0.5, "x.com"), _item("k2", 0.8, "y.com")]
    await frontier.push_batch(items)

    popped = await frontier.pop_next()
    await frontier.record_outcome(popped, "COMPLETED")

    snap = frontier.dump()
    assert len(snap["visited"]) == 1

    f2 = Frontier()
    f2.restore(snap)
    assert f2.contains("k1")
    assert f2.contains("k2")
    assert f2.size == 1


@pytest.mark.asyncio
async def test_snapshot_pending(frontier):
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    item = _item("k1", 1.0)
    item.next_available_at = future
    await frontier.push_batch([item])
    await frontier.pop_next()

    snap = frontier.dump()
    f2 = Frontier()
    f2.restore(snap)
    assert f2.size == 1, "an item waiting on a cooldown survived the round trip"
    assert f2.get_prefilter_context().contains("k1")
    assert f2.contains("k1")
    assert f2.has_seen("k1")


@pytest.mark.asyncio
async def test_pending_retry(frontier):
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
async def test_inflight_once():
    """It is neither waiting nor finished, and dedup has to cover both.

    Discovered again from another page mid-fetch, it would otherwise be
    read twice and analysed twice.
    """
    f = Frontier(domain_budget=0, source=PriorityQueue())
    await f.push_batch([_item("dup", priority=0.9)])
    taken = await f.pop_next(now=datetime.datetime.now(datetime.timezone.utc))
    assert taken is not None

    await f.push_batch([_item("dup", priority=0.9)])
    assert f.size == 0, "the same page came back while it was in flight"
    assert f.get_prefilter_context().contains("dup")


@pytest.mark.asyncio
async def test_checkpoint_old_shape():
    f = Frontier()
    await f.push_batch([_item("old", 0.7)])
    snap = f.dump()
    legacy = {**snap, "ordering": {}, "heap": [_item("old", 0.7).model_dump(mode="json")]}

    restored = Frontier()
    restored.restore(legacy)
    assert restored.size == 1


@pytest.mark.asyncio
async def test_checkpoint_json():
    """The path a real resume takes, which no test walked before.

    In memory dump() and restore() agreed because both spoke models; on
    disk the state is data, and the first thing restore() did with it was
    read an attribute.
    """
    make = PriorityQueue
    f = Frontier(source=make())
    await f.push_batch([_item("a", 0.9), _item("b", 0.5)])

    on_disk = json.loads(json.dumps(f.dump()))
    restored = Frontier(source=make())
    restored.restore(on_disk)

    assert restored.size == 2
    got = await restored.pop_next(now=datetime.datetime.now(datetime.timezone.utc))
    assert got is not None and got.url_key == "a", "priority survived too"


@pytest.mark.asyncio
async def test_cooling_counts(frontier):
    """A pop that returns nothing does not mean there is nothing left.

    The fetch pump reads this to tell a frontier that is empty from one
    whose only item is waiting out a clock, and once read the two the
    same way it ended a run at zero pages with its seed still queued.
    """
    item = _item("k1", 1.0)
    item.next_available_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    await frontier.push_batch([item])
    assert await frontier.pop_next() is None

    assert frontier.cooling == 1
    assert frontier.size == 1


@pytest.mark.asyncio
async def test_refused_idle(frontier):
    """A spent budget refuses the same item forever, so nobody should wait.

    Counting it as cooling would leave the pump asleep on an item that
    is never coming, which is the opposite failure and just as quiet.
    """
    await frontier.push_batch([_item("k1", 1.0)])
    first = await frontier.pop_next(global_budget=1)
    await frontier.record_outcome(first, "COMPLETED")

    await frontier.push_batch([_item("k2", 1.0)])
    assert await frontier.pop_next(global_budget=1) is None, "the budget is spent"
    assert frontier.size == 1
    assert frontier.cooling == 0


@pytest.mark.asyncio
async def test_retiring_empties_both_halves():
    """Left behind, a retired seed's candidates keep the frontier
    non-empty, and a run whose sources have all retired would never read
    as drained."""
    f = Frontier()
    await f.push_candidates([_candidate("a", seed_url_key="dead")])
    await f.push_batch([_item("b", seed_url_key="dead")])
    assert f.size == 1 and f.waiting_size > 0

    f.retire("dead")

    assert f.size == 0
    assert f.waiting_size == 0
    assert f.is_retired("dead")
    assert f.has_seen("a")
    assert not f.contains("a")
    assert not f.contains("b")


@pytest.mark.asyncio
async def test_a_retired_seed_is_not_handed_out():
    f = Frontier()
    await f.push_batch([_item("a", seed_url_key="live"), _item("b", seed_url_key="dead")])
    f.retire("dead")
    got = []
    while (item := await f.pop_next()) is not None:
        got.append(item.seed_url_key)
    assert got == ["live"]


@pytest.mark.parametrize("trigger", ["hungry", "batch", "age", "interrupted"])
async def test_ranking_wait(trigger, monkeypatch):
    """A notification rechecks readiness without exposing the candidate buffer."""
    clock = [0.0]
    monkeypatch.setattr("crawlme.pioneer.buffer.time", SimpleNamespace(monotonic=lambda: clock[0]))
    frontier = Frontier()
    await frontier.push_batch([_item("queued")])
    await frontier.push_candidates([_candidate("first")])
    interrupted = asyncio.Event()
    waiting = asyncio.create_task(frontier.wait_for_ranking(interrupted.is_set))
    try:
        await asyncio.sleep(0)
        await frontier.wake_ranker()
        await asyncio.sleep(0)
        assert not waiting.done(), "a wake alone does not make a batch ready"
        if trigger == "hungry":
            assert await frontier.pop_next() is not None
        elif trigger == "batch":
            await frontier.push_candidates([_candidate(f"c{i}") for i in range(99)])
        elif trigger == "age":
            clock[0] = 31.0
        else:
            interrupted.set()
        await frontier.wake_ranker()
        await asyncio.wait_for(waiting, timeout=1)
        assert frontier.scoring == 0
        batch = await frontier.take_for_ranking(20)
        assert frontier.scoring == len(batch)
        frontier.finish_ranking(batch)
        assert frontier.scoring == 0
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_ranking_already_interrupted():
    frontier = Frontier()
    await frontier.wake_ranker()
    await asyncio.wait_for(frontier.wait_for_ranking(lambda: True), timeout=1)


async def test_outcome_counts_once():
    frontier = Frontier(domain_budget=2)
    await frontier.push_batch([_item("first", 1), _item("second", 0.5)])
    first = await frontier.pop_next()
    await frontier.record_outcome(first, "COMPLETED")
    await frontier.record_outcome(first, "COMPLETED")
    assert frontier.dump()["budgets"]["global"] == 1
    assert (await frontier.pop_next()).url_key == "second"


async def test_retirement_survives_restore():
    frontier = Frontier()
    await frontier.push_candidates([_candidate("ranked", "seed")])
    await frontier.take_for_ranking(1)
    frontier.retire("seed")
    await frontier.push_batch([_item("ranked", seed_url_key="seed")])
    assert frontier.size == 0
    restored = Frontier()
    restored.restore(frontier.dump())
    await restored.push_candidates([_candidate("new", "seed")])
    await restored.push_batch([_item("newer", seed_url_key="seed")])
    assert restored.is_retired("seed")
    assert restored.waiting_size == restored.size == 0


async def test_restore_ranking_ownership():
    frontier = Frontier()
    await frontier.push_candidates([_candidate("first"), _candidate("second")])
    batch = await frontier.take_for_ranking(1)
    restored = Frontier()
    restored.restore(json.loads(json.dumps(frontier.dump())))
    assert restored.waiting_size == 2
    assert restored.scoring == 0
    await restored.push_candidates([_candidate("first"), _candidate("second")])
    returned = await restored.take_for_ranking(2)
    assert {c.url.url_key for c in returned} == {"first", "second"}
    assert returned[0].candidate_id == batch[0].candidate_id


async def test_empty_restore_clears_work():
    frontier = Frontier()
    await frontier.push_candidates([_candidate("stale", "seed")])
    frontier.restore(FrontierSnapshot().model_dump(mode="json"))
    assert frontier.waiting_size == frontier.scoring == 0
    await frontier.push_candidates([_candidate("stale", "seed")])
    assert frontier.waiting_size == 1


async def test_restore_partial_ranking():
    frontier = Frontier()
    await frontier.push_candidates([_candidate("first"), _candidate("second")])
    await frontier.take_for_ranking(2)
    await frontier.push_batch([_item("first")])
    restored = Frontier()
    restored.restore(frontier.dump())
    assert restored.size == restored.waiting_size == 1
    assert (await restored.take_for_ranking(1))[0].url.url_key == "second"
    assert (await restored.pop_next()).url_key == "first"


@pytest.mark.parametrize("cancel", [False, True])
async def test_partial_add_notifies(monkeypatch, cancel, request):
    frontier = Frontier()
    notices = []
    request.addfinalizer(
        subscribe(frontier, lambda owner: owner.waiting_size, lambda: notices.append(frontier.waiting_size))
    )
    entered, release = asyncio.Event(), asyncio.Event()
    add = frontier._waiting.add

    async def interrupted(candidates):
        await add(candidates)
        entered.set()
        await release.wait()
        raise RuntimeError("after queue mutation")

    monkeypatch.setattr(frontier._waiting, "add", interrupted)
    running = asyncio.create_task(frontier.push_candidates([_candidate("partial")]))
    await asyncio.wait_for(entered.wait(), 1)
    if cancel:
        running.cancel()
    else:
        release.set()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await running
    assert frontier.waiting_size == 1
    assert notices == [1]


@pytest.mark.parametrize("ranked", [False, True])
async def test_candidate_query_states(ranked):
    frontier = Frontier()
    assert not frontier.has_seen("state")
    await frontier.push_candidates([_candidate("state")])
    assert frontier.has_seen("state")
    assert not frontier.contains("state")
    batch = await frontier.take_for_ranking(1)
    assert frontier.has_seen("state")
    assert not frontier.contains("state")
    if ranked:
        await frontier.push_batch([_item("state")])
    frontier.finish_ranking(batch)
    assert frontier.has_seen("state")
    assert frontier.contains("state") is ranked
    await frontier.push_candidates([_candidate("state")])
    assert frontier.waiting_size == 0
    if ranked:
        item = await frontier.pop_next()
        assert item is not None
        assert frontier.contains("state")
        await frontier.record_outcome(item, "COMPLETED")
        assert frontier.contains("state")
    restored = Frontier()
    restored.restore(json.loads(json.dumps(frontier.dump())))
    assert restored.has_seen("state")
    assert restored.contains("state") is ranked
    await restored.push_candidates([_candidate("state")])
    assert restored.waiting_size == 0


async def test_ranking_return_dedup():
    frontier = Frontier()
    await frontier.push_candidates([_candidate("return")])
    batch = await frontier.take_for_ranking(1)
    await frontier.return_for_ranking(batch)
    frontier.finish_ranking(batch)
    await frontier.push_candidates([_candidate("return")])
    assert frontier.has_seen("return")
    assert not frontier.contains("return")
    assert (await frontier.take_for_ranking(2)) == batch


@pytest.mark.parametrize("limit, goal_limit", [(1, 0), (0, 1)])
@pytest.mark.parametrize("status", ["COMPLETED", "FAILED"])
async def test_shared_domain_budget(limit, goal_limit, status):
    from crawlme.goals import Goal
    from crawlme.pioneer.prefilter import Decision, PreFilter

    frontier = Frontier(domain_budget=limit)
    goal = Goal(prompt="test", domain_budget=goal_limit)
    candidate = _candidate("later")
    ctx = frontier.get_prefilter_context()
    pf = PreFilter()
    assert pf.check(candidate, goal, ctx)[0] is Decision.ALLOW
    await frontier.push_batch([_item("first"), _item("later", 0.1)])
    first = await frontier.pop_next()
    assert first.url_key == "first"
    assert not frontier.domain_exhausted("example.com")
    await frontier.record_outcome(first, status)
    await frontier.record_outcome(first, status)
    exhausted = limit > 0 and status == "COMPLETED"
    expected = (Decision.DROP, "domain_budget") if exhausted else (Decision.ALLOW, "")
    assert frontier.domain_exhausted("example.com") is exhausted
    assert not frontier.domain_exhausted("other.com")
    assert pf.check(candidate, goal, ctx) == expected
    restored = Frontier(domain_budget=limit)
    restored_ctx = restored.get_prefilter_context()
    restored.restore(json.loads(json.dumps(frontier.dump())))
    # Use a different URL so restored queue dedup does not mask the budget rule.
    assert pf.check(_candidate("new"), goal, restored_ctx) == expected
    for owner in (frontier, restored):
        item = await owner.pop_next()
        assert (item is None) is exhausted
        assert owner.blocked_by_domain_budget == int(exhausted)


def test_prefilter_context_inputs():
    frontier = Frontier()
    allowed = {"example.com"}
    ctx = frontier.get_prefilter_context(allow_fetch=lambda _: False, allowed_domains=allowed)
    assert ctx.allowed_domains == allowed
    assert ctx.allow_fetch("https://example.com") is False
    with pytest.raises(TypeError):
        frontier.get_prefilter_context(visited={"injected"})
