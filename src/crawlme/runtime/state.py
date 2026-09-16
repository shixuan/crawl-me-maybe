"""Run-scoped limits, progress and reporting state, reset in place by the scheduler."""

from __future__ import annotations

import collections
import dataclasses
import datetime
import time
from typing import Any

from crawlme.schemas import CrawlGoal

# How many of a source's own analyzed pages its window keeps.
RELEVANCE_WINDOW = 20
_SEEN_SO_FAR = 5


@dataclasses.dataclass
class Funnel:
    """Per-seed counts at discovery, ranking, fetching and analysis stages."""

    discovered: int = 0
    scored: int = 0
    wanted: int = 0
    fetched: int = 0
    judged: int = 0
    relevant: int = 0

    def as_tuple(self) -> tuple[int, int, int, int, int, int]:
        return (self.relevant, self.judged, self.fetched, self.wanted, self.scored, self.discovered)


@dataclasses.dataclass
class SeedState:
    """One seed's funnel, and whether it is still worth reading."""

    # The address it was given as, kept here so the report can name a
    # seed without a second map from key to URL.
    url: str = ""
    funnel: Funnel = dataclasses.field(default_factory=Funnel)
    # Its own recent content pages, one bool each. Per seed because a
    # feed is productive per account and never as a whole.
    window: collections.deque[bool] = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=RELEVANCE_WINDOW)
    )
    stale: int = 0
    retired: str = ""
    # Pages of this listing already asked for, against _MAX_LISTING_PAGES.
    listing_pages: int = 0


@dataclasses.dataclass
class PageRecord:
    """Join a page seed, listing status and verdict as they arrive independently."""

    seed: str = ""
    listing: bool | None = None
    relevant: bool | None = None
    counted: bool = False

    def ready(self) -> bool:
        """Both halves in, and not counted yet."""
        return self.listing is not None and self.relevant is not None and not self.counted


class PageBook:
    """The pages this run fetched, and the index from address to key.

    The index exists because the analyzer's feedback names a URL while
    everything else is keyed by url_key.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, PageRecord] = {}
        self._key_of: dict[str, str] = {}

    def open(self, url_key: str, canonical: str, seed: str) -> PageRecord:
        """Start a record, before anything is known about the page."""
        self._key_of[canonical] = url_key
        rec = self._by_key.setdefault(url_key, PageRecord())
        rec.seed = seed
        return rec

    def of(self, url_key: str) -> PageRecord:
        return self._by_key.setdefault(url_key, PageRecord())

    def by_url(self, canonical: str) -> PageRecord | None:
        key = self._key_of.get(canonical, "")
        return self._by_key.get(key)

    def seed_of(self, url_key: str, default: str = "") -> str:
        rec = self._by_key.get(url_key)
        return rec.seed if rec and rec.seed else default

    def key_of(self, canonical: str, default: str = "") -> str:
        return self._key_of.get(canonical, default)


@dataclasses.dataclass(frozen=True)
class Limits:
    """Immutable run budgets and goal constraints."""

    max_pages: int = 0
    max_tokens: int = 0
    max_duration_sec: int = 0
    max_relevant: int = 0
    relevance_threshold: float = 0.7
    # Diagnostic mode: nothing the ranker rejects is discarded, it ranks
    # last, and the page budget decides where to stop.
    recall: bool = False
    # since=None leaves the time horizon dormant, which is every run that
    # does not ask for a window.
    since: datetime.datetime | None = None


@dataclasses.dataclass
class Progress:
    """Mutable counters read by stopping policies."""

    pages_fetched: int = 0
    tokens_used: int = 0
    in_flight: int = 0
    relevant_found: int = 0
    started_at: float = 0.0
    # The first failure that was about the run rather than one page.
    fatal_error: str = ""
    # Record the first run-wide platform refusal.
    refused_by: str = ""
    # A platform that changed shape answers every listing and holds
    # nothing, which is not the same as a quiet week.
    listings_seen: int = 0
    listings_empty: int = 0


# Statistics are small enough to live with run state for now.
# They may move to a separate module as their scope grows.
@dataclasses.dataclass
class Stats:
    """Reporting state that stopping policies do not read."""

    links_discovered: int = 0
    candidates_ranked: int = 0
    fetch_errors: int = 0
    analyses_by_class: dict[str, int] = dataclasses.field(default_factory=dict)
    # Count page problems separately from relevance judgments.
    not_content: dict[str, int] = dataclasses.field(default_factory=dict)
    # URLs robots.txt refused. Reported because a run that found nothing
    # because it was not allowed to look is not a quiet week.
    robots_blocked: int = 0
    # Listings read from markup alone. They look like a normal read and
    # carry content weeks older than the account has.
    listings_stale: int = 0

    def reset(self) -> None:
        """Zero every field in place, so stage references stay valid."""
        self.links_discovered = 0
        self.candidates_ranked = 0
        self.fetch_errors = 0
        self.analyses_by_class = {}
        self.not_content = {}
        self.robots_blocked = 0
        self.listings_stale = 0


@dataclasses.dataclass
class RunState:
    """Run data shared by the engine, tracker and reporting; workers receive narrower inputs."""

    limits: Limits
    progress: Progress
    stats: Stats
    pages: PageBook = dataclasses.field(default_factory=PageBook)
    seeds: dict[str, SeedState] = dataclasses.field(default_factory=lambda: collections.defaultdict(SeedState))
    page_contexts: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    relevant_pages: collections.deque[dict[str, Any]] = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=_SEEN_SO_FAR)
    )
    seeds_asked: int | None = None
    rejected_seeds: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    proposed_seeds: dict[str, tuple[str, str]] = dataclasses.field(default_factory=dict)

    def reset(self, *, goal: CrawlGoal, tokens_used_start: int = 0) -> None:
        """Reset execution history while preserving prepared seeds and enhancement metadata.

        Seed ingestion precedes run(), so source URLs and proposal details must
        survive startup. Resume does not call this method.
        """
        self.limits = Limits(
            max_pages=goal.max_pages,
            max_tokens=goal.max_tokens,
            max_duration_sec=goal.max_duration_sec,
            max_relevant=goal.max_relevant,
            relevance_threshold=goal.relevance_threshold,
            recall=goal.recall,
            since=goal.since,
        )
        self.progress = Progress(started_at=time.monotonic(), tokens_used=tokens_used_start)
        self.stats.reset()
        self.pages = PageBook()
        self.page_contexts.clear()
        self.relevant_pages.clear()
        self.seeds = collections.defaultdict(
            SeedState, {key: SeedState(url=seed.url) for key, seed in self.seeds.items() if seed.url}
        )
