"""Run-scoped context: the mutable state every pipeline stage shares.

CrawlContext is the single object of a run that accumulates progress
and statistics as components work.  It is created by the factory and
injected at construction time; the engine resets it in place at the
start of each run, so references held by stages never go stale.

  counters  : thresholds and live progress the stop conditions read
              (unchanged from its life as a standalone CrawlCounters)
  stats     : end-of-run report tallies (discovered, ranked, errors,
              analyses, embedding cache activity)

The context is deliberately a plain dataclass of plain data: it has
one implementation and no behavior to polymorph, so it needs no
protocol.  Future run-scoped concerns (live progress for the status
command, feedback aggregates) become new typed fields here.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import time

from crawlme.schemas import CrawlGoal

#: How many recent analyzed pages the relevance window keeps.
RELEVANCE_WINDOW = 20


@dataclasses.dataclass
class CrawlCounters:
    """Mutable counters shared between the scheduler and stop-condition checks.

    This is the counters half of CrawlContext.  Its values are the only
    thing that persists (as plain dicts in the task row and the frontier
    snapshot), so the dataclass itself is runtime state, not a wire model.
    """

    max_pages: int = 0
    max_tokens: int = 0
    max_duration_sec: int = 0
    relevance_threshold: float = 0.7
    pages_fetched: int = 0
    tokens_used: int = 0
    started_at: float = 0.0
    in_flight: int = 0
    # Candidates that have left the buffer and not yet reached the
    # frontier, i.e. sitting inside a rank call.  They belong to nothing
    # the drain check can see, so without this they read as gone; see
    # _frontier_drained.
    ranking_in_flight: int = 0
    # Sliding window over the most recent analyzed pages, one bool each.
    # A deque with maxlen keeps "recent" true by construction, which is
    # what the DIMINISHING_RETURNS check assumes.
    relevance_window: collections.deque[bool] = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=RELEVANCE_WINDOW)
    )
    fatal_error: str = ""
    # Time horizon (2.8).  since=None keeps TIME_HORIZON dormant, which
    # is every run that does not ask for a window.  stale_streak counts
    # consecutive pages that stated a publication time older than the
    # window; pages that state nothing leave it untouched.
    since: datetime.datetime | None = None
    stale_streak: int = 0
    max_stale_streak: int = 5
    # How many entry points this run was given.  TIME_HORIZON reads it to
    # decide whether "consecutive stale pages" means anything; see the
    # check's own docstring for why anything but 1 leaves it dormant.
    seed_count: int = 0


@dataclasses.dataclass
class RunStats:
    """Tallies the end-of-run report needs; reset in place per run."""

    links_discovered: int = 0
    candidates_ranked: int = 0
    fetch_errors: int = 0
    analyses_by_class: dict[str, int] = dataclasses.field(default_factory=dict)
    embedding_cache_hits: int = 0
    embedding_cache_misses: int = 0

    def reset(self) -> None:
        """Zero every field in place, so stage references stay valid."""
        self.links_discovered = 0
        self.candidates_ranked = 0
        self.fetch_errors = 0
        self.analyses_by_class = {}
        self.embedding_cache_hits = 0
        self.embedding_cache_misses = 0


@dataclasses.dataclass
class CrawlContext:
    """One run's mutable state: stop-condition counters plus report stats."""

    counters: CrawlCounters
    stats: RunStats

    def reset(self, *, goal: CrawlGoal, tokens_used_start: int = 0) -> None:
        """Rebuild the counters for a fresh run and zero the stats.

        The context object itself keeps its identity: components that
        hold a reference from construction time stay connected.  The
        counters field is replaced (they mirror the goal's thresholds,
        which are per-run); the stats object is reset in place.
        """
        self.counters = CrawlCounters(
            max_pages=goal.max_pages,
            max_tokens=goal.max_tokens,
            max_duration_sec=goal.max_duration_sec,
            relevance_threshold=goal.relevance_threshold,
            started_at=time.monotonic(),
            tokens_used=tokens_used_start,
            since=goal.since,
        )
        self.stats.reset()
