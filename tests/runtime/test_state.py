"""Run data ownership and startup reset semantics."""

from __future__ import annotations

from crawlme.runtime.state import Limits, Progress, RunState, SeedState, Stats
from crawlme.schemas import CrawlGoal


def _ctx() -> RunState:
    return RunState(limits=Limits(), progress=Progress(), stats=Stats())


def test_reset_counters():
    ctx = _ctx()
    goal = CrawlGoal(prompt="p", max_pages=7, max_tokens=123, max_duration_sec=60)
    ctx.reset(goal=goal, tokens_used_start=42)

    assert ctx.limits.max_pages == 7
    assert ctx.limits.max_tokens == 123
    assert ctx.limits.max_duration_sec == 60
    assert ctx.progress.tokens_used == 42  # pre-run usage survives the reset
    assert ctx.progress.started_at > 0
    assert ctx.progress.pages_fetched == 0


def test_reset_stats():
    ctx = _ctx()
    ctx.stats.links_discovered = 5
    ctx.stats.analyses_by_class = {"RELEVANT": 2}
    ledger_id = id(ctx.stats)

    ctx.reset(goal=CrawlGoal(prompt="p"))

    assert id(ctx.stats) == ledger_id  # identity preserved, stage refs stay valid
    assert ctx.stats.links_discovered == 0
    assert ctx.stats.analyses_by_class == {}


def test_reset_identity():
    """Startup replaces fields, never the RunState object itself."""
    ctx = _ctx()
    ctx_id = id(ctx)
    ctx.reset(goal=CrawlGoal(prompt="p"))
    assert id(ctx) == ctx_id


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

    state.reset(goal=CrawlGoal(prompt="test"), tokens_used_start=37)

    assert list(state.seeds) == ["seed"]
    assert state.seeds["seed"] == SeedState(url="https://example.com/")
    assert state.pages.by_url("https://example.com/page") is None
    assert not state.page_contexts
    assert not state.relevant_pages
    assert state.seeds_asked == 2
    assert state.proposed_seeds == {"seed": ("https://example.com/", "relevant source")}
    assert state.rejected_seeds == [("https://missing.example/", "unavailable")]
    assert state.progress.tokens_used == 37


def test_states_do_not_share_data():
    first, second = _ctx(), _ctx()
    first.seeds["seed"].url = "https://example.com/"
    first.pages.open("p", "https://example.com/p", "seed")
    first.page_contexts["p"] = {"title": "first"}
    first.relevant_pages.append({"url": "https://example.com/p"})
    first.proposed_seeds["seed"] = ("https://example.com/", "why")
    first.rejected_seeds.append(("https://missing.example/", "unavailable"))
    assert not second.seeds
    assert second.pages.by_url("https://example.com/p") is None
    assert not second.page_contexts
    assert not second.relevant_pages
    assert not second.proposed_seeds
    assert not second.rejected_seeds
