"""Coordinate page file writes and database queuing through Storage."""

from __future__ import annotations

import asyncio
import logging

from crawlme.schemas import FetchResult, Page
from crawlme.storage.contracts import CrawlDb

logger = logging.getLogger(__name__)


class PersistWorker:
    def __init__(self, storage: CrawlDb) -> None:
        self._storage = storage

    async def save_raw(self, url_key: str, result: FetchResult) -> str:
        """Retain HTML even when extraction later fails."""
        path = self._storage.raw_html_path(url_key, result.item_id)
        await asyncio.to_thread(self._storage.save_raw_html, url_key, result.item_id, result.raw)
        return path

    async def save_extracted(self, url_key: str, result: FetchResult, page: Page) -> None:
        page.payload_paths = await asyncio.to_thread(self._save_payloads, url_key, result)
        # The database write queue belongs to the event loop.
        self._storage.save_page(page)

    def _save_payloads(self, url_key: str, result: FetchResult) -> list[str]:
        paths: list[str] = []
        for i, payload in enumerate(result.payloads):
            try:
                paths.append(self._storage.save_payload(url_key, result.item_id, i, payload.body))
            except OSError:
                logger.warning("fetch.payload_unsaved url_key=%s index=%d", url_key, i, exc_info=True)
        return paths
