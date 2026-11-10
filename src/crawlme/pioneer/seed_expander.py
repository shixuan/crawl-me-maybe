"""Discover seeds through platform search or verified model proposals."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from crawlme import prompts
from crawlme.digest.fetcher.base import FetchError
from crawlme.llm import Client, Stage

if TYPE_CHECKING:
    from crawlme.config import Settings
    from crawlme.digest.fetcher.base import Fetcher
    from crawlme.discovery import Harvest
    from crawlme.llm import TokenBudget
    from crawlme.pioneer.models import Candidate, FrontierItem
    from crawlme.platforms.base import SearchAdapter
    from crawlme.runtime import Goal
    from crawlme.urls import Canonicalizer

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


@dataclass
class Expansion:
    candidates: list[Candidate]
    asked: int
    rejected: list[tuple[str, str]]


class SeedExpander:
    """Search for posts and propose sources using a shared seeds-stage client."""

    def __init__(
        self,
        client: Client | None,
        *,
        adapters: tuple[SearchAdapter, ...] = (),
        fetcher_factory: Callable[[], Fetcher] | None = None,
    ) -> None:
        self._client = client
        self._adapters = adapters
        self._fetcher_factory = fetcher_factory

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> SeedExpander:
        """Inert without credentials, like the Goal Enhancer."""
        from crawlme.digest.fetcher import PlaywrightFetcher
        from crawlme.platforms import FEEDS

        has_session = bool(settings.browser_storage_state and Path(settings.browser_storage_state).is_file())
        adapters = tuple(a for a in FEEDS.values() if not a.NEEDS_SESSION or has_session)
        for adapter in FEEDS.values():
            if adapter.NEEDS_SESSION and not has_session:
                logger.info("seeds.search_unavailable platform=%s reason=no_session", adapter.PLATFORM)
        return cls(
            Client.from_settings_if_configured(
                settings,
                budget=budget,
                reasoning_effort=settings.llm_seeds_reasoning_effort,
                stage=Stage.SEEDS,
            ),
            adapters=adapters,
            fetcher_factory=lambda: PlaywrightFetcher(
                storage_state=settings.browser_storage_state if has_session else None,
                timeout=settings.fetch_timeout_read,
                user_agents=list(settings.user_agents),
                keep_payload=lambda url, content_type: any(a.keeps_payload(url, content_type) for a in adapters),
                max_payload_bytes=settings.browser_max_payload_bytes,
                scrolls=min(settings.feed_scrolls, 2),
            ),
        )

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
        if not seeds or want <= 0:
            return Expansion([], 0, [])
        logger.info("expanding %d starting sources, looking for up to %d additional seeds", len(seeds), want)
        searched = await self.search(goal, seeds, want)
        if searched:
            logger.info("seed expansion finished: keeping %d posts from platform search", len(searched))
            return Expansion(searched, len(searched), [])
        logger.info("using model source proposals after platform search")
        proposals = await self.propose(goal, seeds, want)
        if not proposals:
            logger.info("seed expansion finished: no usable source proposals")
            return Expansion([], 0, [])
        logger.info("the model named %d more sources to try", len(proposals))
        candidates, rejected = await verify(proposals, probe=probe, canonicalizer=canonicalizer)
        logger.info("seed expansion finished: %d sources verified, %d rejected", len(candidates), len(rejected))
        return Expansion(candidates, len(proposals), rejected)

    async def search(self, goal: Goal, seeds: list[str], want: int) -> list[Candidate]:
        """Return observed posts; an empty result leaves proposal generation to the caller."""
        if self._fetcher_factory is None or self._client is None or want <= 0:
            logger.info("platform search unavailable: search dependencies are not configured")
            return []
        adapters = [a for a in self._adapters if any(a.claims_url(seed) for seed in seeds)]
        if not adapters:
            logger.info("platform search unavailable: no searchable seed platforms with the required session")
            return []
        from crawlme.llm.errors import RemoteError
        from crawlme.pioneer.models import FrontierItem
        from crawlme.urls import Canonicalizer

        logger.info("generating search queries for %s", ", ".join(a.PLATFORM for a in adapters))
        try:
            response = await self._client.chat(
                f"## Original request\n{goal.prompt}\n\n"
                f"## Enhanced goal (wording aid only)\n{goal.goal_statement or goal.prompt}",
                system=prompts.SEED_QUERIES_SYSTEM,
                json_mode=True,
            )
            data = json.loads(response.content)
            raw = data.get("queries", []) if isinstance(data, dict) else []
            queries = (
                list(dict.fromkeys(q.strip() for q in raw if isinstance(q, str) and 0 < len(q.strip()) <= 160))[:3]
                if isinstance(raw, list)
                else []
            )
        except (ValueError, RemoteError) as exc:
            logger.warning("seeds.search_queries_failed error=%s", exc)
            return []
        if not queries or response.truncated:
            logger.info("seeds.search_fallback reason=invalid_queries")
            return []
        logger.info("generated %d queries: %s", len(queries), " | ".join(" ".join(q.split()) for q in queries))
        if goal.since is not None:
            logger.info(
                "keeping search posts published since %s; undated posts will be skipped", goal.since.isoformat()
            )
        canonicalizer = Canonicalizer()
        seen = {canonicalizer.canonicalize(seed, seed).url_key for seed in seeds}
        batches: list[list[Candidate]] = []
        labels: list[tuple[str, str]] = []
        old = undated = covered = 0
        fetcher = self._fetcher_factory()
        try:
            for adapter in adapters:
                for index, query in enumerate(queries, 1):
                    logger.info(
                        "searching %s (%d/%d): %s", adapter.PLATFORM, index, len(queries), " ".join(query.split())
                    )
                    batch: list[Candidate] = []
                    old_before, undated_before, covered_before = old, undated, covered
                    try:
                        raw_url = adapter.search_url(query)
                        url = canonicalizer.canonicalize(raw_url, raw_url)
                        item = FrontierItem(url=url, url_key=url.url_key, reg_domain=url.reg_domain)
                        result = await asyncio.wait_for(fetcher.fetch(item), timeout=60)
                        html = result.raw.decode("utf-8", errors="replace")
                        refusal = adapter.problem(html)
                        if refusal is not None:
                            logger.info(
                                "seeds.search_unavailable platform=%s reason=%s", adapter.PLATFORM, refusal.value
                            )
                            break
                        posts = adapter.parse_search(html, url.canonical, result.payloads)
                    except Exception as exc:
                        logger.warning("seeds.search_failed platform=%s error=%s", adapter.PLATFORM, exc)
                        break
                    for post in posts:
                        if not post.text.strip():
                            continue
                        if any(adapter.seed_contains(seed, post) for seed in seeds):
                            covered += 1
                            continue
                        if goal.since is not None:
                            if post.published_at is None:
                                undated += 1
                                continue
                            if post.published_at < goal.since:
                                old += 1
                                continue
                        candidate = post.to_candidate(depth=0)
                        candidate.url = canonicalizer.canonicalize(post.permalink, post.permalink)
                        if candidate.url.url_key in seen:
                            continue
                        seen.add(candidate.url.url_key)
                        candidate.seed_ext = True
                        candidate.signals["why"] = f"{adapter.PLATFORM} search: {' '.join(query.split())}"
                        batch.append(candidate)
                    batches.append(batch)
                    labels.append((adapter.PLATFORM, query))
                    logger.info(
                        "%s search (%d/%d): %d posts, %d new candidates, %d too old, %d undated, %d covered by seeds",
                        adapter.PLATFORM,
                        index,
                        len(queries),
                        len(posts),
                        len(batch),
                        old - old_before,
                        undated - undated_before,
                        covered - covered_before,
                    )
        finally:
            await fetcher.aclose()
        counts = [len(batch) for batch in batches]
        quotas = _allocate(counts, want)
        out = []
        for (platform, query), batch, quota in zip(labels, batches, quotas):
            logger.info("search allocation: %s %r, keeping %d of %d candidates", platform, query, quota, len(batch))
            out.extend(batch[:quota])
        logger.info(
            "seeds.search_results usable=%d selected=%d old=%d undated=%d covered=%d",
            sum(counts),
            len(out),
            old,
            undated,
            covered,
        )
        if not out:
            logger.info("seeds.search_fallback reason=no_usable_posts")
        return out

    async def propose(self, goal: Goal, seeds: list[str], want: int) -> list[tuple[str, str]]:
        """Ask for more sources. Pairs of url and the reason given."""
        if self._client is None or want <= 0 or not seeds:
            return []
        prompt = prompts.seeds_input(goal, seeds, want)
        # Log before awaiting the proposal so startup progress is visible.
        logger.info("asking the model for up to %d more sources to try", want)
        resp = await self._client.chat(prompt, system=prompts.SEEDS_SYSTEM, json_mode=True)
        got = _parse(resp.content, known=set(seeds), want=want)
        if not got and resp.truncated:
            # Kept apart from a parser problem. This is a budget to
            # raise, not a prompt to fix.
            logger.warning("seeds.reply_cut_short want=%d; raise LLM_MAX_OUTPUT_TOKENS to use this", want)
        return got


def _allocate(counts: list[int], limit: int) -> list[int]:
    """Allocate proportionally, assigning rounding leftovers by largest remainder."""
    total = sum(counts)
    if total == 0 or limit <= 0:
        return [0] * len(counts)
    limit = min(limit, total)
    quotas = [count * limit // total for count in counts]
    order = sorted(range(len(counts)), key=lambda i: -(counts[i] * limit % total))
    for index in order[: limit - sum(quotas)]:
        quotas[index] += 1
    return quotas


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
