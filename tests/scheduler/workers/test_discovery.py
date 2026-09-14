from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from crawlme.digest.feed.base import FeedDependencyError
from crawlme.digest.harvest import Harvest
from crawlme.scheduler.workers import DiscoveryWorker
from crawlme.schemas import URL, Page


@pytest.mark.asyncio
async def test_listing_metadata_survives():
    harvested = Harvest([], next_url="https://example.com/next", listing=True, degraded=True)
    harvester = MagicMock(harvest=MagicMock(return_value=harvested))
    worker = DiscoveryWorker(harvester, timeout=1)
    page = Page(
        url_key="p",
        url=URL(raw="https://example.com/", canonical="https://example.com/", url_key="p"),
        raw_html_path="saved.html",
        payload_paths=["saved.json"],
    )
    assert await worker.discover(page, 2) is harvested
    harvester.harvest.assert_called_once_with(page, 2)


@pytest.mark.asyncio
async def test_missing_dependency_propagates():
    worker = DiscoveryWorker(MagicMock(harvest=MagicMock(side_effect=FeedDependencyError("missing parser"))), timeout=1)
    with pytest.raises(FeedDependencyError, match="missing parser"):
        await worker.discover(
            Page(url_key="p", url=URL(raw="https://example.com/", canonical="https://example.com/", url_key="p")), 0
        )
