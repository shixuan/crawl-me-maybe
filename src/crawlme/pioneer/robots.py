"""Domain-level fetch policy.

Well, you definitely don't wanna get banned right?

Three mechanisms work together to avoid overloading target servers:

1. robots.txt cache: fetched once per domain, cached for TTL (default 24h).
   `allow_fetch(url)` checks the cached rules before every request.

2. crawl-delay: after a successful fetch, the domain is gated until
   `now + crawl_delay`.  The delay is the max of robots.txt Crawl-delay and
   any adaptive backoff.

3. Circuit breaker: if a domain returns 429/503 more than *circuit_threshold*
   times in a row, it is blocked entirely for *circuit_cooldown* (default 10 min).
   A successful (2xx) response resets the counter.

All three can be bypassed with `ignore=True` (for development / intranet)."""

from __future__ import annotations

import datetime
from urllib.robotparser import RobotFileParser


class RobotsPolicy:
    def __init__(
        self,
        *,
        ignore: bool = False,
        cache_ttl: datetime.timedelta = datetime.timedelta(days=1),
        circuit_threshold: int = 5,
        circuit_cooldown: datetime.timedelta = datetime.timedelta(minutes=10),
    ) -> None:
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
        return rp.can_fetch("*", url)

    def next_allowed_at(self, domain: str) -> datetime.datetime:
        return self._next_allowed.get(domain, _utcnow())

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


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
