"""Propose additional seed URLs with an LLM and verify that they yield candidates."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from crawlme import prompts
from crawlme.digest.fetcher.base import FetchError
from crawlme.llm import LLMClient, LLMError, Stage
from crawlme.logging.progress import activity

if TYPE_CHECKING:
    from crawlme.config import Settings
    from crawlme.discovery.harvester import Harvest
    from crawlme.llm import TokenBudget
    from crawlme.pioneer.canonicalizer import Canonicalizer
    from crawlme.schemas import Candidate, CrawlGoal, FrontierItem

SeedProbe = Callable[["FrontierItem"], Awaitable["Harvest"]]

logger = logging.getLogger(__name__)

# Longest a proposal's reason may be. It is printed back to whoever
# has to judge the seed.
_MAX_WHY = 80

# One verification fetch, generously. A platform seed renders a page.
_VERIFY_TIMEOUT = 60.0


def how_many(given: int, low: int, high: int) -> int:
    """Return zero without seeds; otherwise scale logarithmically within configured bounds."""
    if given <= 0:
        return 0
    return max(low, min(high, round(4 + 2 * math.log2(given))))


class SeedExpander:
    """Propose additional seed URLs with an optional LLM client."""

    def __init__(self, client: LLMClient | None) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> SeedExpander:
        """Inert without credentials, like the Goal Enhancer."""
        return cls(
            LLMClient.from_settings_if_configured(
                settings,
                budget=budget,
                reasoning_effort=settings.llm_enhance_reasoning_effort,
                stage=Stage.SEEDS,
            )
        )

    async def propose(self, goal: CrawlGoal, seeds: list[str], want: int) -> list[tuple[str, str]]:
        """Ask for more sources. Pairs of url and the reason given."""
        if self._client is None or want <= 0 or not seeds:
            return []
        prompt = prompts.seeds_input(goal, seeds, want)
        # Log before awaiting the proposal so startup progress is visible.
        logger.info("asking the model for up to %d more sources to try", want)
        try:
            resp = await self._client.chat(prompt, system=prompts.SEEDS_SYSTEM, json_mode=True)
        except LLMError as e:
            logger.warning("seeds.propose_failed error=%s", e)
            return []
        got = _parse(resp.content, known=set(seeds), want=want)
        if not got and resp.truncated:
            # Kept apart from a parser problem. This is a budget to
            # raise, not a prompt to fix.
            logger.warning("seeds.reply_cut_short want=%d; raise LLM_MAX_OUTPUT_TOKENS to use this", want)
        return got


def _same_place(url: str) -> str:
    """Deduplicate proposed addresses ignoring scheme, www, trailing slash and case.

    This is deliberately broader than canonical URL identity used for crawling."""
    stripped = re.sub(r"^https?://(www\.)?", "", url.strip(), flags=re.I)
    host, _, path = stripped.partition("/")
    return f"{host.lower()}/{path.rstrip('/').lower()}"


def _parse(content: str, *, known: set[str], want: int) -> list[tuple[str, str]]:
    """Read the reply, dropping anything malformed or already known."""
    match = re.search(r"\{.*\}", content or "", re.S)
    if match is None:
        logger.warning("seeds.unparseable")
        return []
    try:
        raw = json.loads(match.group(0)).get("seeds")
    except (json.JSONDecodeError, AttributeError):
        logger.warning("seeds.unparseable")
        return []
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str]] = []
    seen = {_same_place(u) for u in known}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url", "")).strip()
        if not url.startswith(("http://", "https://")) or _same_place(url) in seen:
            continue
        seen.add(_same_place(url))
        out.append((url, str(entry.get("why", "")).strip()[:_MAX_WHY]))
        if len(out) >= want:
            break
    return out


async def verify(
    proposals: list[tuple[str, str]],
    *,
    probe: SeedProbe,
    canonicalizer: Canonicalizer,
) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """Fetch proposals and keep those whose harvester yields candidates.

    The scheduler supplies policy-aware fetching and discovery. Verification checks
    discovery, not relevance; the crawl evaluates relevance after accepting a seed."""
    from crawlme.schemas import Candidate, FrontierItem

    kept: list[Candidate] = []
    # Retain a reason for each rejected proposal.
    dropped: list[tuple[str, str]] = []
    for url, why in proposals:
        logger.info("checking %s", url)
        canonical = canonicalizer.canonicalize(url, url)
        item = FrontierItem(url=canonical, url_key=canonical.url_key, reg_domain=canonical.reg_domain)
        try:
            harvest = await asyncio.wait_for(probe(item), timeout=_VERIFY_TIMEOUT)
        except FetchError as exc:
            dropped.append((url, str(exc)))
            logger.info("dropping %s: %s", url, exc)
            continue
        except Exception:
            logger.info("dropping %s: could not be fetched", url)
            dropped.append((url, "could not be fetched"))
            continue
        if harvest.problem is not None or not harvest.candidates:
            reason = harvest.problem.value if harvest.problem else "nothing to follow"
            logger.info("dropping %s: %s", url, reason)
            dropped.append((url, "does not exist" if harvest.problem else "held nothing to follow"))
            continue
        candidate = Candidate(url=canonical, depth=0, seed_ext=True, signals={"why": why})
        kept.append(candidate)
        logger.info("keeping %s: %s", url, why)
    return kept, dropped


@activity("seed expander")
async def expand(
    goal: CrawlGoal,
    seeds: list[str],
    *,
    settings: Settings,
    budget: TokenBudget | None,
    probe: SeedProbe,
    canonicalizer: Canonicalizer,
) -> tuple[list[Candidate], int, list[tuple[str, str]]]:
    """Return accepted seeds, proposal count and rejected (URL, reason) pairs."""
    want = how_many(len(seeds), settings.expand_seeds_min, settings.expand_seeds_max)
    proposals = await SeedExpander.from_settings(settings, budget=budget).propose(goal, seeds, want)
    if not proposals:
        return [], 0, []
    logger.info("the model named %d more source%s to try", len(proposals), "" if len(proposals) == 1 else "s")
    kept, dropped = await verify(
        proposals,
        probe=probe,
        canonicalizer=canonicalizer,
    )
    logger.info("%d of the %d it named answered", len(kept), len(proposals))
    return kept, len(proposals), dropped
