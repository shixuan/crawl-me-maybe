"""Seed Enhancer: one LLM call per task, at task start.

Turns the seeds a user named into a wider set of the same kind, so a
crawl reaches sources the user did not think of. Off unless asked for.

Same shape as the Goal Enhancer beside it: one call, inert without
credentials, and additive -- the seeds given stay exactly as they were
and keep the larger share of the crawl.

The model is asked where to look, never what is there. Asked for
content it reports what other people said about a source, which is
anti-correlated with what is worth finding; asked for sources, the
crawl still reads them first-hand.

Proposals are not trusted. Measured over four real goals, roughly a
third of what came back did not exist -- brands that are real under
account names that are not. Each one is fetched and read before it is
used, and only what a harvester gets something out of survives.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from typing import TYPE_CHECKING, Any

from crawlme.llm import LLMClient, LLMError

if TYPE_CHECKING:
    from crawlme.config import Settings
    from crawlme.digest.fetcher.base import Fetcher
    from crawlme.digest.harvest import Harvester
    from crawlme.llm import TokenBudget
    from crawlme.schemas import Candidate, CrawlGoal

logger = logging.getLogger(__name__)

# Longest a proposal's reason may be. It is printed back to whoever
# has to judge the seed.
_MAX_WHY = 80

# One verification fetch, generously. A platform seed renders a page.
_VERIFY_TIMEOUT = 60.0

# How many candidates to score. One ranker chunk, so the probe costs
# one call however many links the page held.
_SAMPLE = 20

_SYSTEM = (
    "You propose additional starting points for a web crawler. Given a goal and the "
    "seeds a user already chose, name more sources the crawl would otherwise miss. "
    "Reply with JSON only, no prose: "
    '{"seeds": [{"url": "...", "why": "one short clause"}]}. '
    # No platform is named. What is asked for is who is speaking, which
    # holds for a source of any shape.
    "Prefer sources that publish first-hand -- the party the goal is about, speaking "
    "for itself -- over sites that write about them. You are often wrong about exact "
    "addresses, so name a source only when you are confident it exists. Never repeat a "
    "seed you were given. Give at most the number asked for, and keep every "
    "reason under a dozen words: a long reply is a cut-off reply."
)


def how_many(given: int, low: int, high: int) -> int:
    """How many to ask for, given how many the user named.

    Sub-linear on purpose. One seed still deserves a few, and thirty
    does not need thirty more: past a point the user's own coverage is
    the better guide. Every proposal costs a verification fetch, and
    they share one turn between them however many there are, so more of
    them buys breadth and not attention.
    """
    if given <= 0:
        return 0
    return max(low, min(high, round(4 + 2 * math.log2(given))))


class SeedEnhancer:
    """Propose seeds, then keep the ones a crawl can actually read."""

    def __init__(self, client: LLMClient | None) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> SeedEnhancer:
        """Inert without credentials, like the Goal Enhancer."""
        return cls(
            LLMClient.from_settings_if_configured(
                settings, budget=budget, reasoning_effort=settings.llm_enhance_reasoning_effort
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
    """A key for telling two spellings of one address apart.

    Not the crawl's url_key, which keeps the scheme, the www and the
    path's case because on the open web those can all matter. Here they
    do not: a model asked twice writes the same account four ways, and
    each spelling costs a verification fetch and then a seed the user
    already gave.
    """
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
    ranker: Any = None,
    goal: CrawlGoal | None = None,
) -> list[Candidate]:
    """Keep the proposals a crawl would get something out of.

    Asked by reading one page, not by guessing from the address. A
    platform answers 200 for accounts that do not exist, and the invented
    ones look plausible, so nothing short of fetching separates them.
    What decides is the question the harvester already answers: is there
    anything here to follow.

    Having something to follow is not enough on its own. An events site
    hands back two hundred links to its own help centre, which passes
    that bar and then spends the run going nowhere. So a sample is put
    to the ranker too, and a seed survives only if the ranker wanted at
    least one of them. Measured over one real run, that separates the
    two cleanly: two ext seeds scored nothing at all across every
    candidate they had, while the third scored 68 of 181.

    Payloads are kept and handed on. A grid drops posts as they scroll
    out of view, so an adapter reading markup alone reports a busy
    account as empty, and the seed would be discarded for being real.
    """
    from crawlme.schemas import Candidate, FetchResult, FrontierItem, Page

    kept: list[Candidate] = []
    for url, why in proposals:
        canonical = canonicalizer.canonicalize(url, url)
        item = FrontierItem(url=canonical, url_key=canonical.url_key, reg_domain=canonical.reg_domain)
        try:
            result: FetchResult = await asyncio.wait_for(fetcher.fetch(item), timeout=_VERIFY_TIMEOUT)
        except Exception as e:
            logger.info("seeds.unreachable url=%s error=%s", url, type(e).__name__)
            continue
        page = Page(
            url_key=canonical.url_key,
            url=canonical,
            raw_html_path=storage.save_raw_html(canonical.url_key, result.item_id, result.raw),
            payload_paths=_save_payloads(storage, canonical.url_key, result),
        )
        harvest = harvester.harvest(page, 0)
        if harvest.problem is not None or not harvest.candidates:
            logger.info(
                "seeds.empty url=%s problem=%s",
                url,
                harvest.problem.value if harvest.problem else "nothing to follow",
            )
            continue
        if not await _wanted_by_ranker(harvest.candidates, ranker=ranker, goal=goal):
            logger.info("seeds.off_goal url=%s yields=%d", url, len(harvest.candidates))
            continue
        candidate = Candidate(url=canonical, depth=0, seed_ext=True, signals={"why": why})
        kept.append(candidate)
        logger.info("seeds.kept url=%s yields=%d why=%s", url, len(harvest.candidates), why)
    return kept


def _spread(items: list[Any], n: int) -> list[Any]:
    """A sample drawn across the whole page rather than off the top.

    The top of a listing is its chrome. On the one aggregator that
    turned out to be a good seed, the first eighteen candidates scored
    zero -- header, footer, help links -- and the events it was kept for
    began after them. A head sample would have thrown it away.
    """
    if len(items) <= n:
        return items
    stride = len(items) / n
    return [items[int(i * stride)] for i in range(n)]


async def _wanted_by_ranker(candidates: list[Any], *, ranker: Any, goal: CrawlGoal | None) -> bool:
    """Whether the ranker wanted any of a sample of this page's links.

    Fails open. A ranker that errors or is not configured leaves the
    weaker bar in place, which is the behaviour this had before: a real
    seed is not worth discarding over a provider hiccup.
    """
    if ranker is None or goal is None:
        return True
    from crawlme.pioneer.ranker import DEMOTED_PRIORITY
    from crawlme.schemas import RankHistorySummary

    sample = _spread(candidates, _SAMPLE)
    try:
        decisions = await ranker.rank_batch(goal, sample, RankHistorySummary(goal=goal.goal_statement or goal.prompt))
    except Exception as e:
        logger.info("seeds.unranked error=%s", type(e).__name__)
        return True
    # Above the floor, not above zero. Under --recall a rejection is
    # demoted to a small positive score, and testing for any score at
    # all would let a wholly rejected page through.
    return any(d.priority > DEMOTED_PRIORITY for d in decisions)


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
    ranker: Any = None,
) -> tuple[list[Candidate], int]:
    """Propose seeds and hand back the ones that answered.

    The count of proposals comes back with them, because none surviving
    and none being proposed are different failures and a run that prints
    neither looks like a run that was never asked.
    """
    want = how_many(len(seeds), settings.enhance_seeds_min, settings.enhance_seeds_max)
    proposals = await SeedEnhancer.from_settings(settings, budget=budget).propose(goal, seeds, want)
    if not proposals:
        return [], 0
    logger.info("seeds.proposed count=%d of=%d", len(proposals), want)
    kept = await verify(
        proposals,
        fetcher=fetcher,
        harvester=harvester,
        storage=storage,
        canonicalizer=canonicalizer,
        ranker=ranker,
        goal=goal,
    )
    logger.info("seeds.enhanced kept=%d of=%d", len(kept), len(proposals))
    return kept, len(proposals)
