"""Propose source URLs and verify that they yield crawl candidates."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from crawlme import prompts
from crawlme.digest.fetcher.base import FetchError
from crawlme.llm import Client

if TYPE_CHECKING:
    from crawlme.discovery import Harvest
    from crawlme.goals import Goal
    from crawlme.pioneer.models import Candidate, FrontierItem
    from crawlme.urls import Canonicalizer

SeedProbe = Callable[["FrontierItem"], Awaitable["Harvest"]]

logger = logging.getLogger(__name__)

# Longest a proposal's reason may be. It is printed back to whoever
# has to judge the seed.
_MAX_WHY = 80

# One verification fetch, generously. A platform seed renders a page.
_VERIFY_TIMEOUT = 60.0


@dataclass
class Expansion:
    candidates: list[Candidate]
    asked: int
    rejected: list[tuple[str, str]]


class SeedProposer:
    """Propose source URLs and verify that they yield candidates."""

    def __init__(self, client: Client | None, *, platforms: tuple[str, ...] = ()) -> None:
        self._client = client
        self._platforms = platforms

    async def expand(
        self,
        goal: Goal,
        seeds: list[str],
        want: int,
        *,
        probe: SeedProbe,
        canonicalizer: Canonicalizer,
    ) -> Expansion:
        """Return verified proposals and the reasons for rejected sources."""
        proposals = await self.propose(goal, seeds, want)
        if not proposals:
            logger.info("seed expansion finished: no usable source proposals")
            return Expansion([], 0, [])
        logger.info("the model named %d more sources to try", len(proposals))
        candidates, rejected = await verify(proposals, probe=probe, canonicalizer=canonicalizer)
        logger.info("seed expansion finished: %d sources verified, %d rejected", len(candidates), len(rejected))
        return Expansion(candidates, len(proposals), rejected)

    async def propose(self, goal: Goal, seeds: list[str], want: int) -> list[tuple[str, str]]:
        """Ask for more sources. Pairs of url and the reason given."""
        if self._client is None or want <= 0:
            return []
        prompt = prompts.seeds_input(goal, seeds, want)
        if not seeds and self._platforms:
            prompt += "\n\n## Only propose sources on these platforms\n" + ", ".join(self._platforms)
        # Log before awaiting the proposal so startup progress is visible.
        logger.info("asking the model for up to %d more sources to try", want)
        resp = await self._client.chat(prompt, system=prompts.SEEDS_SYSTEM, json_mode=True)
        got = _parse(resp.content, known=set(seeds), want=want)
        if not seeds and self._platforms:
            from crawlme.platforms import FEEDS

            got = [(url, why) for url, why in got if any(FEEDS[name].claims_url(url) for name in self._platforms)]
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
    from crawlme.pioneer.models import Candidate, FrontierItem

    kept: list[Candidate] = []
    # Retain a reason for each rejected proposal.
    dropped: list[tuple[str, str]] = []
    for index, (url, why) in enumerate(proposals, 1):
        logger.info("checking source (%d/%d): %s", index, len(proposals), url)
        canonical = canonicalizer.canonicalize(url, url)
        item = FrontierItem(url=canonical, url_key=canonical.url_key, reg_domain=canonical.reg_domain)
        try:
            harvest = await asyncio.wait_for(probe(item), timeout=_VERIFY_TIMEOUT)
        except FetchError as exc:
            dropped.append((url, str(exc)))
            logger.info("dropping %s: %s", url, exc)
            continue
        except (TimeoutError, asyncio.TimeoutError):
            logger.info("dropping %s: verification timed out", url)
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
