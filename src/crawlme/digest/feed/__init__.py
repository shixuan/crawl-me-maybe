"""Feed pipeline: platform-neutral vocabulary, plus per-platform markup.

base.py holds what every feed has in common. Each other module here is
one platform's selectors, which no abstraction removes.

FEEDS is the one list of what is supported. The CLI offers its keys as
--feed choices and the factory looks the adapter up by the chosen key,
so a platform is added here and nowhere else, and the flag can never
offer something the factory cannot build.
"""

from crawlme.digest.feed import instagram
from crawlme.digest.feed.base import FeedAdapter, FeedItem, Listing, PageProblem

FEEDS: dict[str, FeedAdapter] = {instagram.PLATFORM: instagram}

__all__ = ["FEEDS", "FeedAdapter", "FeedItem", "Listing", "PageProblem"]
