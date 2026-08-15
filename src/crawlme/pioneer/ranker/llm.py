"""LLMRanker: batched LLM fine-ranking, the final funnel stage (v0.2).

RuleRanker is the relaxed pre-filter; LLMRanker decides.  Each batch of
survivors (at most _BATCH_SIZE per call) is sent to the LLM in a single
request.  The model sees the goal, what the crawl found so far, and the
whole batch at once, so it compares links against each other instead of
judging each in isolation.  The response carries a priority and
rationale per candidate, a drop list for clear junk, and optional new
search suggestions.

Failure policy.  An LLMError (provider failure, token budget exhausted)
propagates: HybridRanker catches it and keeps the earlier stages'
decisions, so a dead LLM never blocks the crawl.  An unparseable JSON
response gets one repair retry with a stricter instruction; if that
also fails, the batch fails the same way.

Partial responses are tolerated fail-open.  Candidates the model did
not mention in either list are kept with a neutral priority, because
the house rule is to over-crawl rather than lose good links.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from crawlme.config import Settings
from crawlme.llm import LLMClient, LLMError, TokenBudget, parse_json_response
from crawlme.schemas import Candidate, CrawlGoal, RankDecision, RankHistorySummary

logger = logging.getLogger(__name__)

# One LLM call covers at most this many candidates; larger survivor
# batches are chunked into sequential calls.
_BATCH_SIZE = 30
# Response cap: 30 rankings with short rationales fit comfortably, and
# the headroom tolerates verbose models without truncation.
_MAX_TOKENS = 4096
# Link texts are truncated so the prompt size stays roughly
# proportional to the batch size; the URL is what mostly matters.
_MAX_FIELD_CHARS = 160
# Priority for candidates the model did not mention at all: kept with a
# neutral score (fail-open, see module docstring).
_NEUTRAL_PRIORITY = 0.5
# At most this many previously-relevant pages are shown to the model.
_MAX_RELEVANT = 5

_SYSTEM = (
    "You are a link priority evaluator for a web crawler. Your task is not to judge "
    "whether a link is relevant, but which link to click first under a limited budget. "
    "You see a batch of candidate links plus the crawl goal and what the crawl found "
    "so far, so compare the candidates against each other, not in isolation. Reply "
    'with JSON only, no prose. Format: {"rankings": [{"id": "<id>", "priority": 0.0, '
    '"rationale": "..."}], "candidates_to_drop": ["<id>"], "new_search_suggestions": '
    '["..."]}. Include every candidate id exactly once, either in rankings or in '
    "candidates_to_drop. rankings holds the candidates to keep: higher priority is "
    "clicked earlier, so use the full 0.0 to 1.0 range. candidates_to_drop holds "
    "clear junk under the goal. If the whole batch is junk, put every id in "
    "candidates_to_drop. new_search_suggestions are optional new search directions "
    "you noticed while judging."
)

_REPAIR_SUFFIX = (
    "\n\nYour previous answer was not valid JSON. Reply with JSON only, no prose, in the exact format requested."
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class LLMRanker:
    """Fine-ranks batches of candidates with one LLM call per batch.

    On provider failure the exception propagates: HybridRanker catches
    it and falls back to the earlier stages' scores, so the pipeline
    never blocks on the LLM.
    """

    def __init__(self, client: LLMClient, batch_size: int = _BATCH_SIZE) -> None:
        self._client = client
        self._batch_size = batch_size

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> LLMRanker | None:
        """Default-on with graceful auto-off: without credentials there
        is nothing to call, so the stage is skipped entirely.  *budget*
        is shared across all LLM consumers of the task."""
        client = LLMClient.from_settings_if_configured(settings, budget=budget)
        return cls(client) if client is not None else None

    async def rank_batch(
        self,
        goal: CrawlGoal,
        candidates: list[Candidate],
        history: RankHistorySummary,
        page_contexts: dict[str, dict[str, Any]] | None = None,
    ) -> list[RankDecision]:
        """Rank all candidates in chunks of _batch_size, one LLM call per chunk."""
        if not candidates:
            return []
        decisions: list[RankDecision] = []
        for start in range(0, len(candidates), self._batch_size):
            chunk = candidates[start : start + self._batch_size]
            decisions.extend(await self._rank_chunk(goal, chunk, history, page_contexts))
        return decisions

    async def aclose(self) -> None:
        """The client pools nothing between calls; provider cleanup is
        the CLI's job at loop teardown."""
        return None

    async def _rank_chunk(
        self,
        goal: CrawlGoal,
        chunk: list[Candidate],
        history: RankHistorySummary,
        page_contexts: dict[str, dict[str, Any]] | None,
    ) -> list[RankDecision]:
        prompt = _build_prompt(goal, chunk, history, page_contexts)
        resp = await self._client.chat(prompt, system=_SYSTEM, max_tokens=_MAX_TOKENS, json_mode=True)
        data = _parse_response(resp.content)
        if data is None:
            logger.warning(
                "llm.rank unparseable json for %d candidates, retrying once with a stricter instruction",
                len(chunk),
            )
            resp = await self._client.chat(
                prompt + _REPAIR_SUFFIX,
                system=_SYSTEM,
                max_tokens=_MAX_TOKENS,
                json_mode=True,
            )
            data = _parse_response(resp.content)
        if data is None:
            raise LLMError(f"unparseable JSON for {len(chunk)} candidates after repair retry")

        tokens = resp.input_tokens + resp.output_tokens
        decisions = _to_decisions(chunk, data, tokens_used=tokens, now=_utcnow())
        kept = sum(1 for d in decisions if not d.dropped)
        logger.info(
            "llm.rank batch=%d kept=%d dropped=%d model=%s tokens=+%d",
            len(chunk),
            kept,
            len(chunk) - kept,
            resp.model,
            tokens,
        )
        return decisions


def _build_prompt(
    goal: CrawlGoal,
    candidates: list[Candidate],
    history: RankHistorySummary,
    page_contexts: dict[str, dict[str, Any]] | None,
) -> str:
    """Assemble the user prompt: goal, prior findings, candidate batch."""
    lines = ["## Goal", goal.prompt]
    if history.relevant_pages:
        lines.append("## Seen so far")
        for entry in history.relevant_pages[:_MAX_RELEVANT]:
            lines.append(f"- {_summarize_page(entry)}")
    lines.append(f"## Candidate links ({len(candidates)})")
    pc = page_contexts or {}
    for c in candidates:
        lines.append(f"{c.candidate_id}: {_trunc(c.url.canonical)}")
        if c.anchor:
            lines.append(f"  anchor: {_trunc(c.anchor)}")
        if c.snippet:
            lines.append(f"  snippet: {_trunc(c.snippet)}")
        if c.parent_heading:
            lines.append(f"  heading: {_trunc(c.parent_heading)}")
        source_title = pc.get(c.source_url_key or "", {}).get("title", "")
        if source_title:
            lines.append(f"  source page: {_trunc(str(source_title))}")
        lines.append(f"  depth: {c.depth}")
    return "\n".join(lines)


def _summarize_page(entry: dict[str, Any]) -> str:
    """One line per prior relevant page, from whatever fields exist."""
    for key in ("title", "url", "summary"):
        value = entry.get(key)
        if value:
            return _trunc(str(value))
    return _trunc(str(entry))


def _trunc(text: str) -> str:
    return text if len(text) <= _MAX_FIELD_CHARS else text[:_MAX_FIELD_CHARS] + "..."


def _parse_response(content: str) -> dict[str, Any] | None:
    """Parse the model's JSON with the shared tolerant parser."""
    return parse_json_response(content)


def _to_decisions(
    candidates: list[Candidate],
    data: dict[str, Any],
    *,
    tokens_used: int,
    now: datetime.datetime,
) -> list[RankDecision]:
    """Turn the parsed response into one decision per candidate.

    Candidates in rankings are kept with the model's priority (clamped
    to [0, 1]); candidates in candidates_to_drop are dropped; ids the
    model did not mention are kept with a neutral priority.
    """
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

    drop_ids: set[str] = set()
    raw_drops = data.get("candidates_to_drop")
    if isinstance(raw_drops, list):
        drop_ids = {d for d in raw_drops if isinstance(d, str) and d}
    drop_ids -= set(scored)  # rankings win when an id lands in both

    known_ids = {c.candidate_id for c in candidates}
    unknown = (set(scored) | drop_ids) - known_ids
    if unknown:
        logger.warning("llm.rank unknown_ids=%s", sorted(unknown))

    suggestions = data.get("new_search_suggestions")
    if isinstance(suggestions, list) and suggestions:
        logger.info("llm.rank suggestions=%s", suggestions)

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
            priority, dropped, rationale = 0.0, True, "llm_drop"
        else:
            priority, dropped, rationale = _NEUTRAL_PRIORITY, False, "no_opinion"
            missing += 1
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
