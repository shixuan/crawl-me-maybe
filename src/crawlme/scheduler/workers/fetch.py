"""Fetch pages and enforce robots policy without persisting page content."""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from crawlme.digest.fetcher import Fetcher
from crawlme.logging import where
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.schemas import URL, FetchResult, FrontierItem
from crawlme.storage.base import Storage

logger = logging.getLogger(__name__)

_ROBOTS_TTL_SECONDS = 86_400
_ROBOTS_TIMEOUT = 15.0


@dataclass(frozen=True)
class FetchFailure:
    reason: Literal["robots", "fetch"]
    error_type: str = ""


class FetchWorker:
    def __init__(
        self,
        fetcher: Fetcher,
        robots: RobotsPolicy,
        storage: Storage,
        *,
        concurrency: int,
    ) -> None:
        self.fetcher = fetcher
        self.robots = robots
        self._storage = storage
        self._slots = asyncio.Semaphore(concurrency)

    async def fetch(self, item: FrontierItem) -> FetchResult | FetchFailure:
        host = (urlparse(item.url.canonical).hostname or "").lower()
        domain = item.url.reg_domain or host
        await self.ensure_robots(host)
        if not self.robots.allow_fetch(item.url.canonical):
            logger.debug("robots.disallowed url=%s domain=%s", item.url.canonical, domain)
            return FetchFailure("robots")
        async with self._slots:
            logger.info("fetching %s", where(item.url.canonical))
            try:
                result = await self.fetcher.fetch(item)
                self.robots.record_response(domain, result.status_code, self.robots.crawl_delay(domain))
            except Exception as e:
                logger.warning("fetch.failed url_key=%s domain=%s depth=%d", item.url_key, domain, item.depth)
                return FetchFailure("fetch", type(e).__name__)
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
        try:
            result = await asyncio.wait_for(self.fetcher.fetch(item), timeout=_ROBOTS_TIMEOUT)
        except Exception as e:
            logger.debug("robots.unreadable domain=%s error=%s", host, e)
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
