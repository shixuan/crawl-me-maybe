"""Prefer platform search, falling back to verified source proposals."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from crawlme.seeds.proposals import Expansion, SeedProbe, SeedProposer
from crawlme.seeds.search import SeedSearch

if TYPE_CHECKING:
    from crawlme.goals import Goal
    from crawlme.urls import Canonicalizer

logger = logging.getLogger(__name__)


class SeedExpander:
    """Choose between searched posts and verified model proposals."""

    def __init__(self, search: SeedSearch, proposer: SeedProposer) -> None:
        self._search = search
        self._proposer = proposer

    async def expand(
        self,
        goal: Goal,
        seeds: list[str],
        want: int,
        *,
        probe: SeedProbe,
        canonicalizer: Canonicalizer,
    ) -> Expansion:
        """Prefer searched posts, otherwise propose and verify additional sources."""
        if want <= 0:
            return Expansion([], 0, [])
        logger.info("expanding %d starting sources, looking for up to %d additional seeds", len(seeds), want)
        searched = await self._search.search(goal, seeds, want)
        if searched:
            logger.info("seed expansion finished: keeping %d posts from platform search", len(searched))
            return Expansion(searched, len(searched), [])
        logger.info("using model source proposals after platform search")
        return await self._proposer.expand(goal, seeds, want, probe=probe, canonicalizer=canonicalizer)
