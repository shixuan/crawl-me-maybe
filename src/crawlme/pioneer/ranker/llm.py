"""Rank and reject candidate batches with an LLM.

Omitted candidates receive neutral priority. Unrecoverable LLM errors propagate
to the scheduler; malformed or truncated replies have bounded recovery paths."""

from __future__ import annotations

import datetime
import logging
from typing import Any

from crawlme import prompts
from crawlme.config import Settings
from crawlme.llm import LLMClient, LLMError, Stage, TokenBudget, parse_json_response
from crawlme.logging import where
from crawlme.schemas import Candidate, CrawlGoal, RankDecision, RankHistorySummary

logger = logging.getLogger(__name__)

# One LLM call covers at most this many candidates; larger survivor
# batches are chunked into sequential calls.
_BATCH_SIZE = 30
# Maximum candidate text per ranking call.
_MAX_BATCH_CHARS = 12_000
# Priority for candidates the model did not mention at all: kept with a
# neutral score (fail-open, see module docstring).
_NEUTRAL_PRIORITY = 0.5
# Retained rejections rank below neutral, unscored candidates.
_DEMOTED_PRIORITY = 0.01
_DROP_TAG = "llm_drop"
_DEMOTED_TAG = "llm_drop_demoted"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class LLMRanker:
    """Rank candidates in batches bounded by count and text size."""

    def __init__(
        self,
        client: LLMClient,
        batch_size: int = _BATCH_SIZE,
        demote_dropped: bool = False,
        max_batch_chars: int = _MAX_BATCH_CHARS,
    ) -> None:
        self._client = client
        self._batch_size = batch_size
        self._demote_dropped = demote_dropped
        self._max_batch_chars = max_batch_chars
        # Lowered whenever a reply runs out of room, so the cost of
        # learning the right size is paid once rather than per batch.
        self._cap = batch_size

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> LLMRanker | None:
        """Default-on with graceful auto-off: without credentials there
        is nothing to call, so the stage is skipped entirely.  *budget*
        is shared across all LLM consumers of the task."""
        # Each LLM stage has an independent reasoning-effort setting.
        client = LLMClient.from_settings_if_configured(
            settings,
            budget=budget,
            reasoning_effort=settings.llm_rank_reasoning_effort,
            stage=Stage.RANKING,
        )
        if client is None:
            return None
        return cls(client, demote_dropped=settings.recall, max_batch_chars=settings.llm_max_batch_chars)

    async def rank_batch(
        self,
        goal: CrawlGoal,
        candidates: list[Candidate],
        history: RankHistorySummary,
        page_contexts: dict[str, dict[str, Any]] | None = None,
    ) -> list[RankDecision]:
        """Rank every candidate, one LLM call per chunk."""
        if not candidates:
            return []
        decisions: list[RankDecision] = []
        for chunk in self._chunks(candidates):
            decisions.extend(await self._rank_chunk(goal, chunk, history, page_contexts))
        return decisions

    def _chunks(self, candidates: list[Candidate]) -> list[list[Candidate]]:
        """Split by candidate count and text size without truncating candidate text."""
        out: list[list[Candidate]] = []
        chunk: list[Candidate] = []
        chars = 0
        for c in candidates:
            size = len(c.text)
            if chunk and (len(chunk) >= min(self._batch_size, self._cap) or chars + size > self._max_batch_chars):
                out.append(chunk)
                chunk, chars = [], 0
            chunk.append(c)
            chars += size
        if chunk:
            out.append(chunk)
        return out

    async def aclose(self) -> None:
        """The client pools nothing between calls; provider cleanup is
        the CLI's job at loop teardown."""
        return None

    async def _halve_batches(self, overran: int) -> None:
        """Remember the size that did not fit, for the batches after this.

        Splitting recovers the batch in hand; without lowering the cap
        the next one is cut to the same size and overruns the same way.
        """
        cap = max(1, min(self._cap, overran) // 2)
        if cap < self._cap:
            logger.warning("llm.rank batch cap %d -> %d after an overrun", self._cap, cap)
            self._cap = cap

    async def _rank_chunk(
        self,
        goal: CrawlGoal,
        chunk: list[Candidate],
        history: RankHistorySummary,
        page_contexts: dict[str, dict[str, Any]] | None,
    ) -> list[RankDecision]:
        prompt = prompts.ranking_input(goal, chunk, history, page_contexts)
        resp = await self._client.chat(prompt, system=prompts.RANKING_SYSTEM, json_mode=True)
        data = _parse_response(resp.content)
        if data is None:
            # Split truncated batches instead of increasing the output ceiling.
            if resp.truncated and len(chunk) > 1:
                await self._halve_batches(len(chunk))
                mid = len(chunk) // 2
                first = await self._rank_chunk(goal, chunk[:mid], history, page_contexts)
                return first + await self._rank_chunk(goal, chunk[mid:], history, page_contexts)
            if resp.truncated:
                # A single truncated candidate cannot be split further.
                logger.warning("llm.rank one candidate overruns the ceiling, retrying with more room")
                resp = await self._client.chat(
                    prompt, system=prompts.RANKING_SYSTEM, max_tokens=resp.output_tokens * 2, json_mode=True
                )
            else:
                logger.warning(
                    "llm.rank unparseable json for %d candidates, retrying once with a stricter instruction",
                    len(chunk),
                )
                resp = await self._client.chat(
                    prompt + prompts.RANKING_REPAIR_SUFFIX, system=prompts.RANKING_SYSTEM, json_mode=True
                )
            data = _parse_response(resp.content)
        if data is None:
            raise LLMError(f"unparseable JSON for {len(chunk)} candidates after repair retry")

        tokens = resp.input_tokens + resp.output_tokens
        decisions = _to_decisions(chunk, data, tokens_used=tokens, now=_utcnow(), demote_dropped=self._demote_dropped)
        kept = sum(1 for d in decisions if not d.dropped)
        logger.debug(
            "llm.rank batch=%d kept=%d %s=%d model=%s tokens=+%d",
            len(chunk),
            kept,
            "demoted" if self._demote_dropped else "dropped",
            sum(1 for d in decisions if (d.rationale or "").startswith(_DROP_TAG)),
            resp.model,
            tokens,
        )
        return decisions


def _parse_response(content: str) -> dict[str, Any] | None:
    """Parse the model's JSON with the shared tolerant parser."""
    return parse_json_response(content)


def _aside(rationale: str) -> str:
    """The model's reason, short enough to sit at the end of a line."""
    if not rationale or rationale == "no_opinion":
        return ""
    trimmed = rationale if len(rationale) <= 60 else rationale[:59] + "\u2026"
    return f", {trimmed}"


def _to_decisions(
    candidates: list[Candidate],
    data: dict[str, Any],
    *,
    tokens_used: int,
    now: datetime.datetime,
    demote_dropped: bool = False,
) -> list[RankDecision]:
    """Produce one decision per candidate, keeping omitted candidates at neutral priority."""
    scored: dict[str, tuple[float, str]] = {}
    raw_rankings = data.get("rankings")
    if isinstance(raw_rankings, list):
        for r in raw_rankings:
            if not isinstance(r, dict):
                continue
            cid = r.get("id")
            if not isinstance(cid, str) or not cid:
                continue
            raw_priority = r.get("priority")
            if isinstance(raw_priority, bool):
                raw_priority = None  # bool is an int subclass; reject it
            if not isinstance(raw_priority, (int, float)):
                continue
            priority = max(0.0, min(1.0, float(raw_priority)))
            rationale = r.get("rationale")
            rationale = str(rationale).strip() if isinstance(rationale, str) else ""
            scored[cid] = (round(priority, 4), rationale)

    # Accept both reason-bearing rejections and legacy bare IDs.
    drops: dict[str, str] = {}
    raw_drops = data.get("candidates_to_drop")
    if isinstance(raw_drops, list):
        for d in raw_drops:
            if isinstance(d, str) and d:
                drops[d] = ""
            elif isinstance(d, dict):
                did = d.get("id")
                if isinstance(did, str) and did:
                    why = d.get("rationale")
                    drops[did] = str(why).strip() if isinstance(why, str) else ""
    for cid in set(scored):
        drops.pop(cid, None)  # rankings win when an id lands in both
    drop_ids = set(drops)

    known_ids = {c.candidate_id for c in candidates}
    unknown = (set(scored) | drop_ids) - known_ids
    if unknown:
        logger.warning("llm.rank unknown_ids=%s", sorted(unknown))

    missing = 0
    decisions: list[RankDecision] = []
    for c in candidates:
        cid = c.candidate_id
        if cid in scored:
            priority, rationale = scored[cid]
            dropped = False
            if not rationale:
                rationale = f"llm_priority={priority:.4f}"
        elif cid in drop_ids:
            # Keep the rejection marker before its reason for downstream counting.
            tag = _DEMOTED_TAG if demote_dropped else _DROP_TAG
            why = drops[cid]
            rationale = f"{tag}: {why}" if why else tag
            priority, dropped = (_DEMOTED_PRIORITY, False) if demote_dropped else (0.0, True)
        else:
            priority, dropped, rationale = _NEUTRAL_PRIORITY, False, "no_opinion"
            missing += 1
        # Log each candidate decision for audit.
        logger.info("scored %.2f %s%s", priority, where(c.url.canonical), _aside(rationale))
        logger.debug("rank.scored url_key=%s priority=%.2f dropped=%s", c.url.url_key, priority, dropped)
        decisions.append(
            RankDecision(
                candidate_id=cid,
                url_key=c.url.url_key,
                priority=priority,
                dropped=dropped,
                ranker="llm",
                rationale=rationale,
                tokens_used=tokens_used,
                decided_at=now,
            )
        )
    if missing:
        logger.warning("llm.rank missing_ids=%d kept at neutral priority", missing)
    return decisions
