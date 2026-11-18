"""Search-first seed expansion and fallback."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from crawlme.discovery import Harvest
from crawlme.goals import Goal
from crawlme.seeds import SeedExpander, SeedProposer, SeedSearch


def _goal() -> Goal:
    return Goal(prompt="new drinks in Toronto")


async def test_expand_fallback_reports_rejected_sources():
    from crawlme.urls import Canonicalizer

    expander = SeedExpander(SeedSearch(None), SeedProposer(None))
    expander._search.search = AsyncMock(return_value=[])
    expander._proposer.propose = AsyncMock(return_value=[("https://example.com/empty", "source")])
    probe = AsyncMock(return_value=Harvest([]))
    result = await expander.expand(_goal(), ["https://example.com/"], 4, probe=probe, canonicalizer=Canonicalizer())
    assert result.candidates == []
    assert result.asked == 1
    assert result.rejected == [("https://example.com/empty", "held nothing to follow")]
    probe.assert_awaited_once()


async def test_expand_cancellation_does_not_fall_back():
    import asyncio

    from crawlme.urls import Canonicalizer

    expander = SeedExpander(SeedSearch(None), SeedProposer(None))
    expander._search.search = AsyncMock(side_effect=asyncio.CancelledError())
    expander._proposer.propose = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await expander.expand(_goal(), ["https://example.com/"], 4, probe=AsyncMock(), canonicalizer=Canonicalizer())
    expander._proposer.propose.assert_not_awaited()


async def test_search_does_not_top_up():
    from unittest.mock import MagicMock

    from crawlme.pioneer import Candidate
    from crawlme.urls import Canonicalizer

    canonicalizer = Canonicalizer()
    candidate = Candidate(url=canonicalizer.canonicalize("https://example.com/post", "https://example.com/post"))
    search = MagicMock(spec=SeedSearch)
    search.search = AsyncMock(return_value=[candidate])
    proposer = MagicMock(spec=SeedProposer)
    probe = AsyncMock()
    result = await SeedExpander(search, proposer).expand(_goal(), [], 4, probe=probe, canonicalizer=canonicalizer)
    assert result.candidates == [candidate]
    assert result.asked == 1
    assert result.rejected == []
    proposer.expand.assert_not_awaited()
    probe.assert_not_awaited()


@pytest.mark.parametrize("want", [0, -1])
async def test_disabled_expansion(want):
    from unittest.mock import MagicMock

    from crawlme.urls import Canonicalizer

    search = MagicMock(spec=SeedSearch)
    proposer = MagicMock(spec=SeedProposer)
    result = await SeedExpander(search, proposer).expand(
        _goal(), [], want, probe=AsyncMock(), canonicalizer=Canonicalizer()
    )
    assert result.candidates == []
    assert result.asked == 0
    assert result.rejected == []
    search.search.assert_not_awaited()
    proposer.expand.assert_not_awaited()
