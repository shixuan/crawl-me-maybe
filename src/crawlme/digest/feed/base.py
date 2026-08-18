"""Feed vocabulary, shared by every platform adapter.

What a feed item is does not vary: a permalink, who posted it, what it
says, and when. Only the markup that carries those varies, and that part
is irreducible, so it lives in a per-platform adapter.

This module holds the half that must not be rewritten per platform,
because rewriting it is how two adapters start disagreeing about what a
post is. Adapters produce FeedItem; the pipeline only ever sees Candidate.

Deliberately no FeedAdapter protocol yet. There is one adapter, so any
carving into methods would be a guess; the shape of the *data* is the
definition of a feed, but the shape of the *interface* is an
implementation detail that should wait for the second platform. See
docs/refactor.md G5.
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass, field
from typing import Any

from crawlme.schemas import URL, Candidate


class PageProblem(str, enum.Enum):
    """Why a fetched page holds no content.

    Platforms answer a wrong or gone identifier with a full, healthy page
    rather than a 404, so this has to be decided from the body. Treating
    those as empty results would read as "quiet this week", every week.
    """

    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    LOGIN_REQUIRED = "login_required"


@dataclass(frozen=True)
class FeedItem:
    """One post, typed at the adapter edge.

    The pipeline never sees this: to_candidate() converts at the
    boundary, so storage and the engine keep one Candidate shape no
    matter which platform produced it. See docs/refactor.md G4.
    """

    permalink: str
    platform: str
    item_id: str = ""
    author: str = ""
    text: str = ""
    published_at: datetime.datetime | None = None
    signals: dict[str, Any] = field(default_factory=dict)

    def to_candidate(self, *, source_url_key: str = "", depth: int = 0) -> Candidate:
        extra: dict[str, Any] = {"platform": self.platform, **self.signals}
        if self.item_id:
            extra["item_id"] = self.item_id
        if self.author:
            extra["account"] = self.author
        if self.published_at:
            extra["posted_at"] = self.published_at.isoformat()
        return Candidate(
            url=URL(
                raw=self.permalink,
                canonical=self.permalink,
                url_key=self.permalink,
                reg_domain=_domain_of(self.permalink),
            ),
            text=self.text,
            signals=extra,
            source_url_key=source_url_key,
            depth=depth,
            discovered_at=_utcnow(),
        )


@dataclass(frozen=True)
class Listing:
    """What a profile or hashtag page yields, split by owner.

    A listing mixes the account's own posts with posts that merely
    mention it, and the latter can outnumber the former. Both are worth
    having, but conflating them lets one monitored account's results bleed
    into another's.

    Items rather than bare permalinks, because a listing carries a weak
    signal worth keeping: who posted, roughly when, and a generated
    description. That is exactly what decides whether a post is worth
    spending a request on.
    """

    own: list[FeedItem] = field(default_factory=list)
    others: list[FeedItem] = field(default_factory=list)

    @property
    def all(self) -> list[FeedItem]:
        return [*self.own, *self.others]


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
