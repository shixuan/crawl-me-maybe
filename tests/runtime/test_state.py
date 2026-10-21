"""Run data ownership and startup reset semantics."""

from __future__ import annotations

import json
from copy import deepcopy

from crawlme.digest import Page
from crawlme.pioneer import FrontierItem
from crawlme.runtime import Goal, Limits, PageState, Progress, RunState, SeedState, Stats
from crawlme.urls import URL


def _ctx() -> RunState:
    return RunState(limits=Limits(), progress=Progress(), stats=Stats())


def _page_state() -> PageState:
    url = URL(raw="https://example.com/p", canonical="https://example.com/p", url_key="p")
    return PageState(item=FrontierItem(url=url, url_key="p"))


def test_reset_counters():
    ctx = _ctx()
    goal = Goal(prompt="p", max_pages=7, max_tokens=123, max_duration_sec=60)
    ctx.reset(goal=goal)

    assert ctx.limits.max_pages == 7
    assert ctx.limits.max_tokens == 123
    assert ctx.limits.max_duration_sec == 60
    assert ctx.progress.started_at > 0
    assert ctx.progress.pages_fetched == 0


def test_reset_stats():
    ctx = _ctx()
    ctx.stats.links_discovered = 5
    ctx.stats.analyses_by_class = {"RELEVANT": 2}
    ledger_id = id(ctx.stats)

    ctx.reset(goal=Goal(prompt="p"))

    assert id(ctx.stats) == ledger_id  # identity preserved, stage refs stay valid
    assert ctx.stats.links_discovered == 0
    assert ctx.stats.analyses_by_class == {}


def test_reset_keeps_prepared_seeds():
    state = _ctx()
    state.seeds["seed"] = SeedState(url="https://example.com/", stale=4, retired="old", listing_pages=3)
    state.seeds["seed"].funnel.fetched = 5
    state.seeds["seed"].window.append(False)
    state.seeds_asked = 2
    state.proposed_seeds["seed"] = ("https://example.com/", "relevant source")
    state.rejected_seeds.append(("https://missing.example/", "unavailable"))
    state.pages.open("page", "https://example.com/page", "seed")
    state.page_contexts["page"] = {"title": "previous"}
    state.relevant_pages.append({"url": "https://example.com/page"})
    page_state = _page_state()
    state.page_states[page_state.item.item_id] = page_state

    state.reset(goal=Goal(prompt="test"))

    assert list(state.seeds) == ["seed"]
    assert state.seeds["seed"] == SeedState(url="https://example.com/")
    assert state.pages.by_url("https://example.com/page") is None
    assert not state.page_contexts
    assert not state.relevant_pages
    assert not state.page_states
    assert state.seeds_asked == 2
    assert state.proposed_seeds == {"seed": ("https://example.com/", "relevant source")}
    assert state.rejected_seeds == [("https://missing.example/", "unavailable")]


def test_states_do_not_share_data():
    first, second = _ctx(), _ctx()
    first.seeds["seed"].url = "https://example.com/"
    first.pages.open("p", "https://example.com/p", "seed")
    first.page_contexts["p"] = {"title": "first"}
    first.relevant_pages.append({"url": "https://example.com/p"})
    first.proposed_seeds["seed"] = ("https://example.com/", "why")
    first.rejected_seeds.append(("https://missing.example/", "unavailable"))
    page_state = _page_state()
    first.page_states[page_state.item.item_id] = page_state
    assert not second.seeds
    assert second.pages.by_url("https://example.com/p") is None
    assert not second.page_contexts
    assert not second.relevant_pages
    assert not second.proposed_seeds
    assert not second.rejected_seeds
    assert not second.page_states


def test_page_progress_roundtrip():
    state = _ctx()
    pending = _page_state()
    fetched = _page_state()
    fetched.page = Page(url=fetched.item.url, url_key=fetched.item.url_key, plain_text="content")
    fetched.status_code = 200
    fetched.response_size = 100
    fetched.analysis_processed = True
    fetched.discovery_recorded = True
    fetched.pagination_processed = True
    state.page_states = {entry.item.item_id: entry for entry in (pending, fetched)}
    snapshot = json.loads(json.dumps(state.dump()))
    original = deepcopy(snapshot)

    restored = _ctx()
    restored.restore(snapshot)

    assert restored.page_states == state.page_states
    assert restored.page_states[fetched.item.item_id].page == fetched.page
    assert snapshot == original
