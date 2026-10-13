"""Discovery results passed to tracking and page processing."""

from dataclasses import dataclass

from crawlme.pioneer import Candidate
from crawlme.platforms import PageProblem


@dataclass(frozen=True)
class Harvest:
    """Candidates, pagination and content-health information from a page."""

    candidates: list[Candidate]
    problem: PageProblem | None = None
    # The rest of a paged listing, enqueued at this page's own depth: it
    # is more of the same listing, not a hop away from it.
    next_url: str = ""
    # Only listings contribute to the empty-listing diagnostic; posts are leaves.
    listing: bool = False
    # The listing was read without the platform's own answer, so it holds
    # whatever the markup had rather than what the account has now.
    degraded: bool = False
