"""Feed vocabulary, shared by every platform adapter.

What a feed item is does not vary: a permalink, who posted it, what it
says, and when. Only the markup that carries those varies, and that part
is irreducible, so it lives in a per-platform adapter.

This module holds the half that must not be rewritten per platform,
because rewriting it is how two adapters start disagreeing about what a
post is. Adapters produce FeedItem; the pipeline only ever sees Candidate.

The FeedAdapter protocol below was deliberately absent while nothing
asked questions of an adapter. What changed is not that a second
platform arrived, but that harvesting needs to ask one ("is this page
yours?"), and answering it inside a per-platform harvester would mean
rewriting the whole collection flow per platform. The protocol carves
only what is called today; the second platform will have opinions, and
that is when to listen to them. See docs/refactor.md G5.
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass, field
from typing import Any, Protocol

from crawlme.schemas import URL, Candidate, Page, Payload


class PageProblem(str, enum.Enum):
    """Why a fetched page holds no content.

    Platforms answer a wrong or gone identifier with a full, healthy page
    rather than a 404, so this has to be decided from the body. Treating
    those as empty results would read as "quiet this week", every week.
    """

    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    LOGIN_REQUIRED = "login_required"

    @property
    def refuses_the_run(self) -> bool:
        """Whether this is about the crawl rather than about one page.

        A gone account is a fact about that account: the other
        twenty-nine are still worth reading.  A block or a dead session
        is a fact about us, and every request after it is wasted at
        best and another strike against the account at worst.

        Written as an exclusion so that a fourth kind stops the run
        until somebody decides it should not.  Being loud about an
        unfamiliar refusal is the cheaper mistake.
        """
        return self is not PageProblem.UNAVAILABLE


@dataclass(frozen=True)
class FeedItem:
    """One post, typed at the adapter edge.

    The pipeline never sees this: to_candidate() converts at the
    boundary, so storage and the engine keep one Candidate shape no
    matter which platform produced it. See docs/refactor.md G4.
    """

    permalink: str
    platform: str
    item_id: str = ""
    author: str = ""
    text: str = ""
    published_at: datetime.datetime | None = None
    signals: dict[str, Any] = field(default_factory=dict)

    def to_candidate(self, *, source_url_key: str = "", depth: int = 0) -> Candidate:
        extra: dict[str, Any] = {"platform": self.platform, **self.signals}
        if self.item_id:
            extra["item_id"] = self.item_id
        if self.author:
            extra["account"] = self.author

        # The permalink as stated. A harvester canonicalizes it before
        # the candidate goes anywhere, which is what gives it the same
        # url_key shape as the rest of the crawl.
        return Candidate(
            url=URL(
                raw=self.permalink,
                canonical=self.permalink,
                url_key=self.permalink,
                reg_domain=_domain_of(self.permalink),
            ),
            text=self.text,
            posted_at=self.published_at,
            signals=extra,
            source_url_key=source_url_key,
            depth=depth,
            discovered_at=_utcnow(),
        )


@dataclass(frozen=True)
class Listing:
    """What a profile or hashtag page yields, split by owner.

    A listing mixes the account's own posts with posts that merely
    mention it, and the latter can outnumber the former. Both are worth
    having, but conflating them lets one monitored account's results bleed
    into another's.

    Items rather than bare permalinks, because a listing carries a weak
    signal worth keeping: who posted, roughly when, and a generated
    description. That is exactly what decides whether a post is worth
    spending a request on.
    """

    own: list[FeedItem] = field(default_factory=list)
    others: list[FeedItem] = field(default_factory=list)

    @property
    def all(self) -> list[FeedItem]:
        return [*self.own, *self.others]


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class FeedAdapter(Protocol):
    """One platform's answer to "what is on this page?".

    Everything platform-shaped lives behind this: which host the platform
    serves, how it says a page is gone, and how its markup carries a
    listing or a single item. Everything platform-neutral — deciding a
    page is not ours, turning items into candidates, marking who posted
    what — stays in the harvester, written once.
    """

    #: Platform name, stamped onto every candidate's signals.
    PLATFORM: str
    #: Registrable domain the platform serves, used to tell its own pages
    #: from anything a crawl wandered onto.
    DOMAIN: str

    #: Whether reading this platform at all requires a logged-in
    #: session.  A crawl of a walled platform without one fetches login
    #: pages and reports them as a platform with nothing on it.
    NEEDS_SESSION: bool

    def claims_url(self, url: str) -> bool:
        """Whether this URL is ours, judged before anything is fetched.

        Weaker than claims() on purpose: some platforms are recognisable
        from the address and some are not, and a run has to be refused
        before it starts rather than after it has paid for a page.  An
        adapter that cannot tell answers False, and is simply not
        consulted at that point.
        """
        ...

    def claims(self, page: Page) -> bool:
        """Whether this page is one of ours.

        Asked of the adapter rather than decided outside it, because
        what makes a page a platform's page is exactly the kind of
        knowledge an adapter exists to hold: a domain for one platform,
        a document's root element for another.

        A page nobody claims is not an error.  It is a page, and a page
        with links on it is what a link graph reads.
        """
        ...

    def problem(self, html: str) -> PageProblem | None:
        """Why this page holds no content, or None if it does."""
        ...

    def keeps_payload(self, url: str, content_type: str) -> bool:
        """Whether a response the page fetched is worth keeping.

        Answered per platform because only the platform knows which of
        its own requests carries the posts. Answering False to everything
        is valid and costs nothing: a platform whose text is already in
        the document has no use for this.
        """
        ...

    def parse_listing(self, html: str, url: str, payloads: list[Payload]) -> Listing:
        """Read a listing page into items, split by who posted them.

        Takes the page URL, not an account: reading one out of the other
        is as platform-shaped as the markup.

        `payloads` is what the page fetched for itself, and is empty
        whenever nothing kept it -- a plain HTTP fetch, or a run that did
        not ask. An adapter must still return its best answer from the
        markup alone in that case, so richer text is an upgrade and never
        a requirement.
        """
        ...

    def parse_item(self, html: str, url: str) -> FeedItem | None:
        """Read a single-item page, or None if this is not one."""
        ...
