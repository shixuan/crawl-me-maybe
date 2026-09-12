"""Seed Enhancer: one LLM call per task, at task start.

Turns the seeds a user named into a wider set of the same kind, so a
crawl reaches sources the user did not think of. Off unless asked for,
and additive: the seeds given stay as they were and keep the larger
share of the crawl.

The model is asked where to look, never what is there. Asked for
content it reports what other people said about a source, which is
anti-correlated with what is worth finding.

Proposals are not trusted. Measured over four real goals, roughly a
third of what came back did not exist, real brands under account names
that are not. Each is fetched and read before it is used, and only what
a harvester gets something out of survives.
"""

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
    from crawlme.digest.harvest import Harvester
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
    # No platform is named, and neither is a kind of organisation. What
    # is asked for is what a source publishes, which holds for a source
    # of any shape. Asked instead for the party the goal is about, it
    # answered a non-food goal with coffee chains three times running.
    "Judge a source by what it posts, not by who it is: name it only if its own recent "
    "posts would themselves be answers to the goal. An account that exists to post "
    "exactly this beats a brand that merely does it sometimes, and beats a directory "
    "that indexes everyone. You are often wrong about exact "
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
        # Said before the wait, not after it. This one call has taken
        # over two minutes on a thinking model, and a line that only
        # arrives with the answer leaves the terminal silent for all of
        # it, which reads as a hang.
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
) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """Keep the proposals a crawl would get something out of.

    Asked by reading one page, not by guessing from the address. A
    platform answers 200 for accounts that do not exist and the invented
    ones look plausible, so nothing short of fetching separates them.
    What decides is what the harvester already answers: is there
    anything here to follow.

    Whether it is worth reading past that is not asked. It was once, and
    on a sample of twenty captions the answer was wrong whenever the
    fetch came back thin: three real accounts turned away on three
    leftover posts each. Retiring a source asks the same question later,
    on pages actually read.

    Payloads are kept and handed on. A grid drops posts as they scroll
    out of view, so an adapter reading markup alone reports a busy
    account as empty and the seed would be discarded for being real.
    """
    from crawlme.schemas import Candidate, FetchResult, FrontierItem, Page

    kept: list[Candidate] = []
    # What was turned away and what turned it away. A proposal costs a
    # fetch either way, and the reason is the only thing that says
    # whether the model guessed an address or picked a poor source.
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
    """Propose seeds and hand back the ones that answered.

    The count of proposals comes back with them, because none surviving
    and none being proposed are different failures and a run that prints
    neither looks like a run that was never asked.
    """
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
