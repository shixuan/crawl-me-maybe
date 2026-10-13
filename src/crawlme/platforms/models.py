"""Platform listing entries and content problems."""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass, field
from typing import Any

from crawlme.pioneer import Candidate
from crawlme.urls import URL


class PageProblem(str, enum.Enum):
    """Content-level failures reported by platform adapters."""

    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    LOGIN_REQUIRED = "login_required"

    @property
    def refuses_the_run(self) -> bool:
        """Treat unavailable pages as local failures and other refusals as run-wide."""
        return self is not PageProblem.UNAVAILABLE


@dataclass(frozen=True)
class FeedItem:
    """Adapter output converted to Candidate at the harvester boundary."""

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

        # The harvester canonicalizes this URL before enqueueing.
        return Candidate(
            url=URL(
                raw=self.permalink,
                canonical=self.permalink,
                url_key=self.permalink,
                reg_domain=_domain_of(self.permalink),
            ),
            text=self.text,
            posted_at=self.published_at,
            signals=extra,
            source_url_key=source_url_key,
            depth=depth,
            discovered_at=_utcnow(),
        )


@dataclass(frozen=True)
class Listing:
    """Listing entries split between the source owner and other authors."""

    own: list[FeedItem] = field(default_factory=list)
    others: list[FeedItem] = field(default_factory=list)
    # Optional continuation URL for paged listings.
    next_url: str = ""
    # The adapter reports potentially incomplete or stale listing content.
    degraded: bool = False

    @property
    def all(self) -> list[FeedItem]:
        return [*self.own, *self.others]


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
