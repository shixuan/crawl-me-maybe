"""Feed pipeline: platform-neutral vocabulary, plus per-platform markup.

base.py holds what every feed has in common. Each other module here is
one platform's selectors, which no abstraction removes.
"""

from crawlme.digest.feed.base import FeedItem, Listing, PageProblem

__all__ = ["FeedItem", "Listing", "PageProblem"]
