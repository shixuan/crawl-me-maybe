"""Manual URL source: parses comma-separated --seeds argument."""

from __future__ import annotations

from crawlme.pioneer.sources.base import _make_candidate
from crawlme.schemas import Candidate, CrawlGoal


class ManualSource:
    def __init__(self, seeds: list[str]) -> None:
        self._seeds = seeds

    async def discover(self, goal: CrawlGoal) -> list[Candidate]:
        return [_make_candidate(u) for u in self._seeds if u.strip()]
