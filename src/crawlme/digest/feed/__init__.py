"""Feed adapter registry and shared feed types."""

from crawlme.digest.feed import instagram, reddit, rss
from crawlme.digest.feed.base import FeedAdapter, FeedItem, Listing, PageProblem

FEEDS: dict[str, FeedAdapter] = {instagram.PLATFORM: instagram, reddit.PLATFORM: reddit}

# Adapter order is priority; the first claimant handles the page.
ADAPTERS: tuple[FeedAdapter, ...] = (instagram, reddit, rss)

__all__ = ["ADAPTERS", "FEEDS", "FeedAdapter", "FeedItem", "Listing", "PageProblem"]
