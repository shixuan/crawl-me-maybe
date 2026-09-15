"""Discover candidates from saved page inputs with a bounded parsing wait."""

from __future__ import annotations

import asyncio
import logging

from crawlme.digest.harvest import Harvest, Harvester
from crawlme.schemas import Page

logger = logging.getLogger(__name__)


class DiscoveryWorker:
    def __init__(self, harvester: Harvester, *, timeout: float) -> None:
        self.harvester = harvester
        self._timeout = timeout

    async def discover(self, page: Page, depth: int) -> Harvest:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self.harvester.harvest, page, depth), timeout=self._timeout)
        except asyncio.TimeoutError:
            # The await is bounded; Python cannot stop an already-running parser thread.
            logger.warning("fetch.link_timeout url_key=%s", page.url_key)
            return Harvest([])
