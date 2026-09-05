"""Run-scoped context: the mutable state every pipeline stage shares.

CrawlContext is the single object of a run that accumulates progress
and statistics as components work.  It is created by the factory and
injected at construction time; the engine resets it in place at the
start of each run, so references held by stages never go stale.

  counters  : thresholds and live progress the stop conditions read
              (unchanged from its life as a standalone CrawlCounters)
  stats     : end-of-run report tallies (discovered, ranked, errors,
              analyses)

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

# How many of a source's own analyzed pages its window keeps.
RELEVANCE_WINDOW = 20


@dataclasses.dataclass
class Funnel:
    """How far this seed's work got, stage by stage.

    Monotonically decreasing by construction, so every gap between two
    stages means something and no new question needs a new field:
    discovered but not scored is a rotation that never reached it,
    wanted but not fetched is a budget that ran out first, and fetched
    but not judged is a run that stopped before the analyzer answered.
    That last gap is why "read 7 pages, wanted 6" once reported nothing
    and read as a content judgement when six of the seven were never
    looked at.
    """

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
    """What the run knows about one page it fetched.

    Three facts arrive from three places and in no fixed order: the seed
    when the page is dispatched, whether it was a listing after it is
    harvested, and the verdict when the analyzer answers, which for a
    retried analysis can be long after both. Held as one record rather
    than one dict each, so "are both halves in" is a question about a
    record instead of a lookup in two maps.
    """

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
    """What the run was told it may spend, and what counts as an answer.

    Fixed when the run starts. Separate from Progress because a ceiling
    and a tally read the same way in code and mean opposite things: one
    is an instruction, the other is what happened.
    """

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
    """What the run has done so far, as the stop conditions read it.

    Every field here is read by some stop condition. That is the entry
    rule: a number nothing stops on belongs in the Ledger, or the run
    grows counters that look like stopping criteria and are not.
    """

    pages_fetched: int = 0
    tokens_used: int = 0
    in_flight: int = 0
    relevant_found: int = 0
    started_at: float = 0.0
    # The first failure that was about the run rather than one page.
    fatal_error: str = ""
    # The first page problem that was about the crawl rather than about
    # one page. A block or a dead session makes every later request
    # wasted, so one is enough to end the run.
    refused_by: str = ""
    # A platform that changed shape answers every listing and holds
    # nothing, which is not the same as a quiet week.
    listings_seen: int = 0
    listings_empty: int = 0


@dataclasses.dataclass
class Ledger:
    """Everything the report reads and no stop condition does.

    Kept apart from Progress on purpose. A number here can be added,
    renamed or grouped differently without touching a stop condition,
    and check_stop is not handed this object, so the separation holds by
    signature rather than by discipline.
    """

    links_discovered: int = 0
    candidates_ranked: int = 0
    fetch_errors: int = 0
    analyses_by_class: dict[str, int] = dataclasses.field(default_factory=dict)
    # Pages that came back as something other than content, by kind.
    # Reported because a run that read thirty accounts and found three
    # of them gone is a different run from one that found none gone.
    not_content: dict[str, int] = dataclasses.field(default_factory=dict)
    # URLs robots.txt refused. Reported because a run that found nothing
    # because it was not allowed to look is not a quiet week.
    robots_blocked: int = 0
    # Listings read from markup alone. They look like a normal read and
    # carry content weeks older than the account has.
    listings_stale: int = 0
    # Per seed, how far its work got and whether it is still worth
    # reading. Keyed by the seed's url_key.
    seeds: dict[str, SeedState] = dataclasses.field(default_factory=lambda: collections.defaultdict(SeedState))

    def reset(self) -> None:
        """Zero every field in place, so stage references stay valid."""
        self.links_discovered = 0
        self.candidates_ranked = 0
        self.fetch_errors = 0
        self.analyses_by_class = {}
        self.not_content = {}
        self.robots_blocked = 0
        self.listings_stale = 0
        self.seeds = collections.defaultdict(SeedState)


@dataclasses.dataclass
class CrawlContext:
    """One run's state, split by who reads it.

    Limits are the instruction, Progress is what the stop conditions
    read, and the Ledger is what only the report reads. Two of them used
    to be one class, which is how a statistic nothing stops on ended up
    sitting among the stopping criteria.
    """

    limits: Limits
    progress: Progress
    ledger: Ledger

    def reset(self, *, goal: CrawlGoal, tokens_used_start: int = 0) -> None:
        """Rebuild for a fresh run, keeping this object's identity.

        Components hold a reference from construction time, so the
        container stays and its parts are replaced or zeroed.
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
        self.ledger.reset()
