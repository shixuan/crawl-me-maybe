"""Platform adapters for recognition, access requirements and content discovery."""

from crawlme.adapters import instagram, reddit, rss
from crawlme.adapters.base import FeedAdapter, FeedItem, Listing, PageProblem

FEEDS: dict[str, FeedAdapter] = {instagram.PLATFORM: instagram, reddit.PLATFORM: reddit}

# Adapter order is priority; the first claimant handles the page.
ADAPTERS: tuple[FeedAdapter, ...] = (instagram, reddit, rss)

__all__ = ["ADAPTERS", "FEEDS", "FeedAdapter", "FeedItem", "Listing", "PageProblem"]
