"""URL Source protocol: produces seed Candidates from external inputs."""

from __future__ import annotations

import datetime
from typing import Protocol

from crawlme.schemas import URL, Candidate, CrawlGoal


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class UrlSource(Protocol):
    """Protocol for seed URL discovery.

    Each implementation produces depth=0 Candidates that the scheduler
    canonicalizes, pre-filters, and enqueues into the frontier."""

    async def discover(self, goal: CrawlGoal) -> list[Candidate]: ...


def _make_candidate(raw_url: str) -> Candidate:
    url = URL(raw=raw_url, canonical=raw_url, url_key=raw_url)
    return Candidate(url=url, depth=0, discovered_at=_utcnow())
