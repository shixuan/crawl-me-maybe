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

#: Every adapter, in the order they are asked whether a page is theirs.
#: Order is priority: the first to claim it does the reading.  One entry
#: makes that moot, and stating it now is cheaper than discovering later
#: that two adapters silently disagreed about the same page.
ADAPTERS: tuple[FeedAdapter, ...] = (instagram,)

__all__ = ["ADAPTERS", "FEEDS", "FeedAdapter", "FeedItem", "Listing", "PageProblem"]
