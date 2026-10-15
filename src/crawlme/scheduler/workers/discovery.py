"""Discover candidates from saved page inputs with a bounded parsing wait."""

from __future__ import annotations

import asyncio

from crawlme.digest import Page
from crawlme.discovery import Harvest, Harvester
from crawlme.errors import ErrorHandler, Skipped


class DiscoveryWorker:
    def __init__(self, harvester: Harvester, *, timeout: float, error_handler: ErrorHandler) -> None:
        self.error_handler = error_handler
        self.harvester = harvester
        self._timeout = timeout

    async def discover(self, page: Page, depth: int) -> Harvest:
        result = await self.error_handler.call(
            "discovery",
            lambda: asyncio.wait_for(asyncio.to_thread(self.harvester.harvest, page, depth), timeout=self._timeout),
            optional=True,
        )
        return Harvest([]) if isinstance(result, Skipped) else result
