"""File URL source: reads seeds from a JSON file.

Supports two formats:
  Bare list:    ["https://a.com", "https://b.com"]
  Seeds+scope:  {"seeds": ["https://a.com"], "allowed_domains": ["a.com"]}
"""

from __future__ import annotations

import json
from pathlib import Path

from crawlme.pioneer.sources.base import _make_candidate
from crawlme.schemas import Candidate, CrawlGoal


class FileSource:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._allowed_domains: set[str] | None = None

    @property
    def allowed_domains(self) -> set[str] | None:
        return self._allowed_domains

    async def discover(self, goal: CrawlGoal) -> list[Candidate]:
        data = json.loads(self._path.read_text())
        if isinstance(data, list):
            urls = data
        elif isinstance(data, dict):
            urls = data.get("seeds", [])
            domains = data.get("allowed_domains")
            if isinstance(domains, list):
                self._allowed_domains = set(domains)
        else:
            return []
        return [_make_candidate(u) for u in urls if isinstance(u, str) and u.strip()]
