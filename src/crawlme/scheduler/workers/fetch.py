"""Fetch pages and enforce robots policy without persisting page content."""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from crawlme.digest import Fetcher, FetchResult
from crawlme.errors import Error, ErrorHandler, Skipped
from crawlme.logging import where
from crawlme.logging.progress import activity
from crawlme.persist import Storage
from crawlme.pioneer import FrontierItem
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.urls import URL

logger = logging.getLogger(__name__)

_ROBOTS_TTL_SECONDS = 86_400
_ROBOTS_TIMEOUT = 15.0


@dataclass(frozen=True)
class FetchFailure:
    reason: Literal["robots", "fetch"]
    error: Error | None = None

    @property
    def error_type(self) -> str:
        return type(self.error).__name__ if self.error is not None else ""


class FetchWorker:
    def __init__(
        self,
        fetcher: Fetcher,
        robots: RobotsPolicy,
        storage: Storage,
        *,
        concurrency: int,
        error_handler: ErrorHandler,
        max_attempts: int = 3,
    ) -> None:
        self.error_handler = error_handler
        self._max_attempts = max_attempts
        self.fetcher = fetcher
        self.robots = robots
        self._storage = storage
        self._slots = asyncio.Semaphore(concurrency)

    @activity("fetch")
    async def fetch(self, item: FrontierItem) -> FetchResult | FetchFailure:
        host = (urlparse(item.url.canonical).hostname or "").lower()
        domain = item.url.reg_domain or host
        await self.ensure_robots(host)
        if not self.robots.allow_fetch(item.url.canonical):
            logger.debug("robots.disallowed url=%s domain=%s", item.url.canonical, domain)
            return FetchFailure("robots")
        async with self._slots:
            now = datetime.datetime.now(datetime.timezone.utc)
            delay = (self.robots.next_allowed_at(domain) - now).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            logger.info("fetching %s", where(item.url.canonical))
            attempts = 0

            async def attempt() -> FetchResult:
                nonlocal attempts
                attempts += 1
                result = await self.fetcher.fetch(item)
                result.fetch_attempt = attempts
                return result

            result = await self.error_handler.call(
                "fetch",
                attempt,
                optional=True,
                repeatable=True,
                max_attempts=self._max_attempts,
            )
            if isinstance(result, Skipped):
                logger.warning("fetch.failed url_key=%s error=%s", item.url_key, result.error)
                return FetchFailure("fetch", result.error)
            self.robots.record_response(domain, result.status_code, self.robots.crawl_delay(host))
            return result

    async def ensure_robots(self, host: str) -> None:
        """Load policy by hostname, which may differ from the budget domain."""
        if not host or not self.robots.is_cache_stale(host):
            return
        cached = await self._storage.get_robots(host)
        if cached and not _robots_expired(cached):
            self.robots.load_robots_txt(host, str(cached.get("raw", "")))
            return
        raw = await self._fetch_robots(host)
        self.robots.load_robots_txt(host, raw)
        self._storage.save_robots(
            {
                "domain": host,
                "raw": raw,
                "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "ttl": _ROBOTS_TTL_SECONDS,
            }
        )

    async def _fetch_robots(self, host: str) -> str:
        """Unavailable policies become empty rules, preserving fail-open behavior."""
        url = f"https://{host}/robots.txt"
        item = FrontierItem(url=URL(raw=url, canonical=url, url_key=url, reg_domain=host), url_key=url, reg_domain=host)
        result = await self.error_handler.call(
            "robots",
            lambda: asyncio.wait_for(self.fetcher.fetch(item), timeout=_ROBOTS_TIMEOUT),
            optional=True,
            repeatable=True,
            max_attempts=self._max_attempts,
        )
        if isinstance(result, Skipped):
            return ""
        if result.status_code >= 400:
            return ""
        return result.raw.decode("utf-8", errors="replace")

    async def aclose(self) -> None:
        await self.fetcher.aclose()


def _robots_expired(cached: dict[str, Any]) -> bool:
    try:
        fetched = datetime.datetime.fromisoformat(str(cached.get("fetched_at", "")))
    except ValueError:
        return True
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=datetime.timezone.utc)
    ttl = float(cached.get("ttl") or _ROBOTS_TTL_SECONDS)
    return (datetime.datetime.now(datetime.timezone.utc) - fetched).total_seconds() > ttl
