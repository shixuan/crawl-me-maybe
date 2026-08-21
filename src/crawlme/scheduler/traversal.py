"""One row per way of traversing a source, and every choice named in it.

The crawler grew up on a link graph, so every default it has is a link
graph's default. Feeds arrived later and inherited all of them silently,
and each one had to be found by running into it:

  - a per-domain ceiling, which on one platform is a total one
  - a domain reputation factor, constant where every post shares a host
  - a time horizon that ends a run, right only for one ordered walk
  - a depth limit of five, where a listing and its posts are two
  - a factor set scoring five of its seven signals on constants

Five separate discoveries, one design fact: nothing listed what a
traversal chooses. This is that list. Adding a platform means filling a
row, not finding out which defaults were written for something else.

Deliberately data and nothing else. The moment a row can carry behaviour
it becomes the place every awkward special case goes, and the table stops
being readable in one screen -- which was the whole point of having it.

Fields have no defaults on purpose: a new row that forgets one fails at
import rather than quietly inheriting a link graph's answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from crawlme.digest.feed import FEEDS, FeedAdapter
from crawlme.digest.feed import instagram as _instagram
from crawlme.pioneer.ranker.rule import FEED_FACTORS, GRAPH_FACTORS, Factor


@dataclass(frozen=True)
class Traversal:
    """Everything one kind of source decides differently."""

    #: What --feed calls it, and what Settings.source_kind holds.
    name: str

    #: The platform behind a feed, or None for a link graph.  Decides the
    #: harvester, and which of a page's own requests are worth keeping.
    adapter: FeedAdapter | None

    #: Which signals the rule stage scores on.  A post carries its own
    #: text and no anchor, path shape or position in a page.
    factors: tuple[Factor, ...]

    #: Pages one domain may contribute, or 0 for no ceiling.  A ceiling
    #: protects a graph crawl from one site absorbing the run; on a feed
    #: every candidate shares the platform's host, so the same number is
    #: a total that silently overrides the page budget.
    domain_budget: int

    #: How many times to ask a listing for more of itself.  A listing
    #: hands out one screen, so a window of weeks otherwise sees a dozen
    #: posts.  Zero for a graph: nothing below the fold is waiting.
    scrolls: int

    #: How far from a seed the crawl may go.  A listing and its posts are
    #: two levels and there is no third; a graph is a graph.
    depth_limit: int

    #: Whether running out of recent content may end the whole run.  It
    #: reads the first stale page as evidence that everything after it is
    #: stale too, which holds only when one walk is ordered by time.
    time_horizon: bool


TRAVERSALS: dict[str, Traversal] = {
    "links": Traversal(
        name="links",
        adapter=None,
        factors=GRAPH_FACTORS,
        domain_budget=50,
        scrolls=0,
        depth_limit=5,
        time_horizon=True,
    ),
    "instagram": Traversal(
        name="instagram",
        adapter=_instagram,
        factors=FEED_FACTORS,
        domain_budget=0,
        scrolls=4,
        depth_limit=1,
        time_horizon=False,
    ),
}

#: The link graph, and what anything unrecognised falls back to.
DEFAULT = TRAVERSALS["links"]


def traversal_for(source_kind: str) -> Traversal:
    """The row for a source kind, or the link graph if it names none."""
    return TRAVERSALS.get(source_kind, DEFAULT)


def feed_kinds() -> list[str]:
    """Source kinds that read a platform, for --feed to offer."""
    return sorted(name for name, t in TRAVERSALS.items() if t.adapter is not None)


__all__ = ["DEFAULT", "FEEDS", "TRAVERSALS", "Traversal", "feed_kinds", "traversal_for"]
