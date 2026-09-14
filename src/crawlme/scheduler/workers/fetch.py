"""Fetch, persist and extract pages without changing frontier or run counters."""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from crawlme.digest.extractor import Extractor
from crawlme.digest.fetcher import Fetcher
from crawlme.logging import where
from crawlme.pioneer.robots import RobotsPolicy
from crawlme.schemas import URL, FetchResult, FrontierItem, Page
from crawlme.storage.contracts import CrawlDb

logger = logging.getLogger(__name__)

_ROBOTS_TTL_SECONDS = 86_400
_ROBOTS_TIMEOUT = 15.0


@dataclass(frozen=True)
class FetchedPage:
    result: FetchResult
    page: Page


@dataclass(frozen=True)
class FetchFailure:
    reason: Literal["robots", "fetch", "extract_timeout"]
    error_type: str = ""


class FetchWorker:
    def __init__(
        self,
        fetcher: Fetcher,
        extractor: Extractor,
        robots: RobotsPolicy,
        storage: CrawlDb,
        *,
        concurrency: int,
        extract_timeout: float,
    ) -> None:
        self.fetcher = fetcher
        self.extractor = extractor
        self.robots = robots
        self._storage = storage
        self._slots = asyncio.Semaphore(concurrency)
        self._extract_timeout = extract_timeout

    async def fetch(self, item: FrontierItem) -> FetchedPage | FetchFailure:
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
            raw_path = self._storage.raw_html_path(item.url_key, result.item_id)
            logger.debug("fetch.extracting url_key=%s size=%dKB", item.url_key, len(result.raw) // 1024)
            await asyncio.to_thread(self._storage.save_raw_html, item.url_key, result.item_id, result.raw)
            try:
                page = await asyncio.wait_for(
                    asyncio.to_thread(self.extractor.extract, result, raw_path), timeout=self._extract_timeout
                )
            except asyncio.TimeoutError:
                logger.warning("fetch.extract_timeout url_key=%s size=%dKB", item.url_key, len(result.raw) // 1024)
                return FetchFailure("extract_timeout")
            page.payload_paths = await asyncio.to_thread(self._save_payloads, item.url_key, result)
            self._storage.save_page(page)
            return FetchedPage(result, page)

    def _save_payloads(self, url_key: str, result: FetchResult) -> list[str]:
        paths: list[str] = []
        for i, payload in enumerate(result.payloads):
            try:
                paths.append(self._storage.save_payload(url_key, result.item_id, i, payload.body))
            except OSError:
                logger.warning("fetch.payload_unsaved url_key=%s index=%d", url_key, i, exc_info=True)
        return paths

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
