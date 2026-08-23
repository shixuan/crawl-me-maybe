"""Tests for the run context: the mutable ball every stage shares."""

from __future__ import annotations

from crawlme.schemas import CrawlGoal
from crawlme.state.context import CrawlContext, CrawlCounters, RunStats


def _ctx() -> CrawlContext:
    return CrawlContext(counters=CrawlCounters(), stats=RunStats())


def test_reset_rebuilds_counters_from_goal():
    ctx = _ctx()
    goal = CrawlGoal(prompt="p", max_pages=7, max_tokens=123, max_duration_sec=60)
    ctx.reset(goal=goal, tokens_used_start=42)

    assert ctx.counters.max_pages == 7
    assert ctx.counters.max_tokens == 123
    assert ctx.counters.max_duration_sec == 60
    assert ctx.counters.tokens_used == 42  # pre-run usage survives the reset
    assert ctx.counters.started_at > 0
    assert ctx.counters.pages_fetched == 0


def test_reset_zeroes_stats_in_place():
    ctx = _ctx()
    ctx.stats.links_discovered = 5
    ctx.stats.analyses_by_class = {"RELEVANT": 2}
    stats_id = id(ctx.stats)

    ctx.reset(goal=CrawlGoal(prompt="p"))

    assert id(ctx.stats) == stats_id  # identity preserved, stage refs stay valid
    assert ctx.stats.links_discovered == 0
    assert ctx.stats.analyses_by_class == {}


def test_reset_keeps_context_identity():
    """The engine replaces fields, never the context object itself."""
    ctx = _ctx()
    ctx_id = id(ctx)
    ctx.reset(goal=CrawlGoal(prompt="p"))
    assert id(ctx) == ctx_id
