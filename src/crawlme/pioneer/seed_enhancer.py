"""Propose additional seed URLs with an LLM and verify that they yield candidates."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from typing import TYPE_CHECKING, Any

from crawlme.llm import LLMClient, LLMError, Stage

if TYPE_CHECKING:
    from crawlme.config import Settings
    from crawlme.digest.fetcher.base import Fetcher
    from crawlme.discovery.harvester import Harvester
    from crawlme.llm import TokenBudget
    from crawlme.schemas import Candidate, CrawlGoal

logger = logging.getLogger(__name__)

# Longest a proposal's reason may be. It is printed back to whoever
# has to judge the seed.
_MAX_WHY = 80

# One verification fetch, generously. A platform seed renders a page.
_VERIFY_TIMEOUT = 60.0

_SYSTEM = (
    "You propose additional starting points for a web crawler. Given a goal and the "
    "seeds a user already chose, name more sources the crawl would otherwise miss. "
    "Reply with JSON only, no prose: "
    '{"seeds": [{"url": "...", "why": "one short clause"}]}. '
    # Ask for sources that publish relevant content without fixing a platform or organization type.
    "Judge a source by what it posts, not by who it is: name it only if its own recent "
    "posts would themselves be answers to the goal. An account that exists to post "
    "exactly this beats a brand that merely does it sometimes, and beats a directory "
    "that indexes everyone. You are often wrong about exact "
    "addresses, so name a source only when you are confident it exists. Never repeat a "
    "seed you were given. Give at most the number asked for, and keep every "
    "reason under a dozen words: a long reply is a cut-off reply."
)


def how_many(given: int, low: int, high: int) -> int:
    """Return zero without seeds; otherwise scale logarithmically within configured bounds."""
    if given <= 0:
        return 0
    return max(low, min(high, round(4 + 2 * math.log2(given))))


class SeedEnhancer:
    """Propose additional seed URLs with an optional LLM client."""

    def __init__(self, client: LLMClient | None) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> SeedEnhancer:
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
        prompt = (
            f"## Goal\n{goal.goal_statement or goal.prompt}\n\n"
            f"## Seeds already chosen\n" + "\n".join(f"- {s}" for s in seeds) + f"\n\n## Give at most {want}"
        )
        # Log before awaiting the proposal so startup progress is visible.
        logger.info("asking the model for up to %d more sources to try", want)
        try:
            resp = await self._client.chat(prompt, system=_SYSTEM, json_mode=True)
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
    fetcher: Fetcher,
    harvester: Harvester,
    storage: Any,
    canonicalizer: Any,
) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """Fetch proposals and keep those whose harvester yields candidates.

    Retain sub-responses for adapters that need them. Verification checks discovery,
    not relevance; the crawl evaluates relevance after accepting a seed."""
    from crawlme.schemas import Candidate, FetchResult, FrontierItem, Page

    kept: list[Candidate] = []
    # Retain a reason for each rejected proposal.
    dropped: list[tuple[str, str]] = []
    for url, why in proposals:
        logger.info("checking %s", url)
        canonical = canonicalizer.canonicalize(url, url)
        item = FrontierItem(url=canonical, url_key=canonical.url_key, reg_domain=canonical.reg_domain)
        try:
            result: FetchResult = await asyncio.wait_for(fetcher.fetch(item), timeout=_VERIFY_TIMEOUT)
        except Exception:
            logger.info("dropping %s: could not be fetched", url)
            dropped.append((url, "could not be fetched"))
            continue
        page = Page(
            url_key=canonical.url_key,
            url=canonical,
            raw_html_path=storage.save_raw_html(canonical.url_key, result.item_id, result.raw),
            payload_paths=_save_payloads(storage, canonical.url_key, result),
        )
        harvest = harvester.harvest(page, 0)
        if harvest.problem is not None or not harvest.candidates:
            reason = harvest.problem.value if harvest.problem else "nothing to follow"
            logger.info("dropping %s: %s", url, reason)
            dropped.append((url, "does not exist" if harvest.problem else "held nothing to follow"))
            continue
        candidate = Candidate(url=canonical, depth=0, seed_ext=True, signals={"why": why})
        kept.append(candidate)
        logger.info("keeping %s: %s", url, why)
    return kept, dropped


def _save_payloads(storage: Any, url_key: str, result: Any) -> list[str]:
    paths: list[str] = []
    for i, payload in enumerate(result.payloads):
        try:
            paths.append(storage.save_payload(url_key, result.item_id, i, payload.body))
        except OSError as e:
            logger.debug("seeds.payload_unsaved url_key=%s error=%s", url_key, e)
    return paths


async def enhance(
    goal: CrawlGoal,
    seeds: list[str],
    *,
    settings: Settings,
    budget: TokenBudget | None,
    fetcher: Fetcher,
    harvester: Harvester,
    storage: Any,
    canonicalizer: Any,
) -> tuple[list[Candidate], int, list[tuple[str, str]]]:
    """Return accepted seeds, proposal count and rejected (URL, reason) pairs."""
    want = how_many(len(seeds), settings.enhance_seeds_min, settings.enhance_seeds_max)
    proposals = await SeedEnhancer.from_settings(settings, budget=budget).propose(goal, seeds, want)
    if not proposals:
        return [], 0, []
    logger.info("the model named %d more source%s to try", len(proposals), "" if len(proposals) == 1 else "s")
    kept, dropped = await verify(
        proposals,
        fetcher=fetcher,
        harvester=harvester,
        storage=storage,
        canonicalizer=canonicalizer,
    )
    logger.info("%d of the %d it named answered", len(kept), len(proposals))
    return kept, len(proposals), dropped
