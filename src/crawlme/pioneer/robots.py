"""Cache robots rules and enforce crawl delays and rate-limit cooldowns.

The scheduler fetches robots.txt. ignore=True bypasses these policies."""

from __future__ import annotations

import datetime
from urllib.robotparser import RobotFileParser


class RobotsPolicy:
    def __init__(
        self,
        *,
        agent: str = "*",
        ignore: bool = False,
        cache_ttl: datetime.timedelta = datetime.timedelta(days=1),
        circuit_threshold: int = 5,
        circuit_cooldown: datetime.timedelta = datetime.timedelta(minutes=10),
    ) -> None:
        # Match robots rules using the configured crawler product token.
        self._agent = agent
        self._ignore = ignore
        self._cache_ttl = cache_ttl
        self._circuit_threshold = circuit_threshold
        self._circuit_cooldown = circuit_cooldown
        self._parsers: dict[str, RobotFileParser] = {}
        self._fetched_at: dict[str, datetime.datetime] = {}
        self._next_allowed: dict[str, datetime.datetime] = {}
        self._consecutive_fails: dict[str, int] = {}
        self._circuit_until: dict[str, datetime.datetime] = {}

    def load_robots_txt(self, domain: str, raw: str) -> None:
        if self._ignore:
            return
        rp = RobotFileParser()
        rp.parse(raw.splitlines())
        self._parsers[domain] = rp
        self._fetched_at[domain] = _utcnow()

    def allow_fetch(self, url: str) -> bool:
        if self._ignore:
            return True
        domain = _extract_domain(url)
        if not domain:
            return True
        until = self._circuit_until.get(domain)
        if until is not None and _utcnow() < until:
            return False
        rp = self._parsers.get(domain)
        if rp is None:
            return True
        return rp.can_fetch(self._agent, url)

    def crawl_delay(self, domain: str) -> float:
        """Return the delay requested by this domain for the configured crawler agent."""
        if self._ignore:
            return 0.0
        rp = self._parsers.get(domain)
        if rp is None:
            return 0.0
        stated = rp.crawl_delay(self._agent)
        if stated is None:
            return 0.0
        try:
            return max(0.0, float(stated))
        except ValueError:
            return 0.0

    def next_allowed_at(self, domain: str) -> datetime.datetime:
        # Epoch leaves unknown domains immediately eligible, avoiding races with caller time.
        return self._next_allowed.get(domain, _EPOCH)

    def record_response(self, domain: str, status: int, crawl_delay: float = 0) -> None:
        if self._ignore:
            return
        now = _utcnow()
        if status in (429, 503):
            self._consecutive_fails[domain] = self._consecutive_fails.get(domain, 0) + 1
            fails = self._consecutive_fails[domain]
            if fails >= self._circuit_threshold:
                self._circuit_until[domain] = now + self._circuit_cooldown
            self._next_allowed[domain] = now + datetime.timedelta(seconds=min(2**fails, 300))
        elif 200 <= status < 300:
            self._consecutive_fails[domain] = 0
            if crawl_delay > 0:
                self._next_allowed[domain] = now + datetime.timedelta(seconds=crawl_delay)

    def is_cache_stale(self, domain: str) -> bool:
        if self._ignore:
            return False
        fetched = self._fetched_at.get(domain)
        if fetched is None:
            return True
        return (_utcnow() - fetched) > self._cache_ttl


def _extract_domain(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower()


_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
