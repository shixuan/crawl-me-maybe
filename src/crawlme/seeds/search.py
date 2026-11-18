"""Search platforms and select eligible seed posts."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING

from crawlme import prompts
from crawlme.llm import Client, RemoteError

if TYPE_CHECKING:
    from crawlme.digest.fetcher.base import Fetcher
    from crawlme.goals import Goal
    from crawlme.pioneer.models import Candidate
    from crawlme.platforms.base import SearchAdapter

logger = logging.getLogger(__name__)


class SeedSearch:
    """Search selected platforms and allocate eligible posts across queries."""

    def __init__(
        self,
        client: Client | None,
        *,
        adapters: tuple[SearchAdapter, ...] = (),
        platforms: tuple[str, ...] = (),
        search_count: int = 3,
        missing_session: str | None = None,
        fetcher_factory: Callable[[], Fetcher] | None = None,
    ) -> None:
        self._client = client
        self._adapters = adapters
        self._platforms = platforms
        self._search_count = search_count
        self._missing_session = missing_session
        self._fetcher_factory = fetcher_factory

    async def search(self, goal: Goal, seeds: list[str], want: int) -> list[Candidate]:
        """Return observed posts; an empty result leaves proposal generation to the caller."""
        if self._fetcher_factory is None or self._client is None or want <= 0:
            logger.info("platform search unavailable: search dependencies are not configured")
            return []
        adapters = self._select_adapters(seeds)
        if not adapters:
            logger.info("platform search unavailable: no searchable seed platforms with the required session")
            return []
        from crawlme.pioneer.models import FrontierItem
        from crawlme.urls import Canonicalizer

        logger.info("generating search queries for %s", ", ".join(a.PLATFORM for a in adapters))
        queries = await _queries(self._client, goal, self._search_count)
        if not queries:
            return []
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

    def _select_adapters(self, seeds: list[str]) -> list[SearchAdapter]:
        adapters = [
            a
            for a in self._adapters
            if (any(a.claims_url(seed) for seed in seeds) if seeds else a.PLATFORM in self._platforms)
        ]
        if self._missing_session is not None:
            available = []
            for adapter in adapters:
                if not adapter.NEEDS_SESSION:
                    available.append(adapter)
                    continue
                path = shlex.quote(self._missing_session or f"./{adapter.PLATFORM}-session.json")
                logger.warning(
                    "%s search unavailable: no session file found. "
                    "Create one with: crawl session %s --feed %s\n"
                    "Log in in the browser, then rerun with --session %s. "
                    "Continuing without this platform's search; source proposals remain available as fallback.",
                    adapter.PLATFORM,
                    path,
                    adapter.PLATFORM,
                    path,
                )
            adapters = available
        return adapters


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


async def _queries(client: Client, goal: Goal, count: int) -> list[str]:
    try:
        response = await client.chat(
            f"## Original request\n{goal.prompt}\n\n"
            f"## Enhanced goal (wording aid only)\n{goal.goal_statement or goal.prompt}",
            system=f"Generate at most {count} queries. " + prompts.SEED_QUERIES_SYSTEM,
            json_mode=True,
        )
        data = json.loads(response.content)
        raw = data.get("queries", []) if isinstance(data, dict) else []
        queries = (
            list(dict.fromkeys(q.strip() for q in raw if isinstance(q, str) and 0 < len(q.strip()) <= 160))[:count]
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
    return queries
