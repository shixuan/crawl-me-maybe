"""RSS/Atom URL source — extracts links from feed entries.

Requires feedparser (optional — raises ImportError if not installed).
"""

from __future__ import annotations

from crawlme.pioneer.sources.base import _make_candidate
from crawlme.schemas import Candidate, CrawlGoal


class RssSource:
    def __init__(self, url: str) -> None:
        self._url = url

    async def discover(self, goal: CrawlGoal) -> list[Candidate]:
        try:
            import feedparser
        except ImportError:
            raise ImportError("RssSource requires feedparser: pip install feedparser") from None

        feed = feedparser.parse(self._url)
        candidates: list[Candidate] = []
        for entry in feed.entries:
            link = entry.get("link", "")
            if link:
                candidates.append(_make_candidate(link))
        return candidates
