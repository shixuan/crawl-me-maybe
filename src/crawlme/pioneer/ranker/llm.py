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
# Link texts are truncated so the prompt size stays roughly
# proportional to the batch size; the URL is what mostly matters.
_MAX_FIELD_CHARS = 160
# Room for a batch's texts.  Sixty real posts came to 40k characters in
# total, so this holds a normal batch whole and splits an unusual one
# into more calls rather than into fragments.  Each extra call repeats
# only the system prompt, which is a rounding error next to the text.
_MAX_BATCH_CHARS = 12_000
# Priority for candidates the model did not mention at all: kept with a
# neutral score (fail-open, see module docstring).
_NEUTRAL_PRIORITY = 0.5
# Below anything the model scores itself, so a rejection is read last
# rather than not at all.  Not zero: a candidate nobody has an opinion
# about should still outrank one the model argued against.
_DEMOTED_PRIORITY = 0.01
_DROP_TAG = "llm_drop"
_DEMOTED_TAG = "llm_drop_demoted"
# At most this many previously-relevant pages are shown to the model.
_MAX_RELEVANT = 5

_SYSTEM = (
    "You are a link priority evaluator for a web crawler. Your task is not to judge "
    "whether a link is relevant, but which link to click first under a limited budget. "
    "You see a batch of candidate links plus the crawl goal and what the crawl found "
    "so far, so compare the candidates against each other, not in isolation. Reply "
    'with JSON only, no prose. Format: {"rankings": [{"id": "<id>", "priority": 0.0, '
    '"rationale": "..."}], "candidates_to_drop": [{"id": "<id>", "rationale": "..."}], '
    '"new_search_suggestions": '
    '["..."]}. Include every candidate id exactly once, either in rankings or in '
    "candidates_to_drop. rankings holds the candidates to keep: higher priority is "
    "clicked earlier, so use the full 0.0 to 1.0 range. candidates_to_drop holds "
    "clear junk under the goal, each with a short rationale saying what makes it "
    "junk. If the whole batch is junk, put every id in candidates_to_drop. "
    "new_search_suggestions are optional new search directions "
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

    def __init__(self, client: LLMClient, batch_size: int = _BATCH_SIZE, demote_dropped: bool = False) -> None:
        self._client = client
        self._batch_size = batch_size
        self._demote_dropped = demote_dropped

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> LLMRanker | None:
        """Default-on with graceful auto-off: without credentials there
        is nothing to call, so the stage is skipped entirely.  *budget*
        is shared across all LLM consumers of the task."""
        client = LLMClient.from_settings_if_configured(settings, budget=budget)
        return cls(client, demote_dropped=settings.recall) if client is not None else None

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
        """Split into calls by count and by how much text they carry.

        A candidate is never split across the boundary, and never shown
        in part: whatever it says, the model sees all of it or waits for
        the next call.  Truncating each candidate instead is what a
        char cap does, and it fails the same way at every size -- a post
        whose one relevant line sits past the cut is rejected for not
        containing what was cut off.  It cost a run three real results
        at 160 characters, and would have cost fewer but not none at 800.

        Chunking by text is what makes that affordable: one long post
        takes room from its batch rather than from its own content.
        """
        out: list[list[Candidate]] = []
        chunk: list[Candidate] = []
        chars = 0
        for c in candidates:
            size = len(c.text)
            if chunk and (len(chunk) >= self._batch_size or chars + size > _MAX_BATCH_CHARS):
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

    async def _rank_chunk(
        self,
        goal: CrawlGoal,
        chunk: list[Candidate],
        history: RankHistorySummary,
        page_contexts: dict[str, dict[str, Any]] | None,
    ) -> list[RankDecision]:
        prompt = _build_prompt(goal, chunk, history, page_contexts)
        resp = await self._client.chat(prompt, system=_SYSTEM, json_mode=True)
        data = _parse_response(resp.content)
        if data is None:
            # A reply that used the whole ceiling was cut off mid-JSON,
            # and no amount of stricter wording buys the room to finish
            # it.  Asking again the same way just spends the ceiling
            # twice, which is what a run on a reasoning model did before
            # falling back to embedding-only scores.
            if resp.truncated:
                logger.warning(
                    "llm.rank hit the output ceiling for %d candidates, retrying with more room",
                    len(chunk),
                )
                resp = await self._client.chat(
                    prompt,
                    system=_SYSTEM,
                    max_tokens=resp.output_tokens * 2,
                    json_mode=True,
                )
            else:
                logger.warning(
                    "llm.rank unparseable json for %d candidates, retrying once with a stricter instruction",
                    len(chunk),
                )
                resp = await self._client.chat(prompt + _REPAIR_SUFFIX, system=_SYSTEM, json_mode=True)
            data = _parse_response(resp.content)
        if data is None:
            raise LLMError(f"unparseable JSON for {len(chunk)} candidates after repair retry")

        tokens = resp.input_tokens + resp.output_tokens
        decisions = _to_decisions(chunk, data, tokens_used=tokens, now=_utcnow(), demote_dropped=self._demote_dropped)
        kept = sum(1 for d in decisions if not d.dropped)
        logger.info(
            "llm.rank batch=%d kept=%d %s=%d model=%s tokens=+%d",
            len(chunk),
            kept,
            "demoted" if self._demote_dropped else "dropped",
            sum(1 for d in decisions if (d.rationale or "").startswith(_DROP_TAG)),
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
        if c.text:
            # Whole, not truncated: this is what the candidate says, and
            # the batch is sized so it fits.  The cap below still guards
            # the proxies a link carries, which are short by nature.
            lines.append(f"  text: {c.text}")
        if c.anchor:
            lines.append(f"  anchor: {_trunc(c.anchor)}")
        if c.snippet:
            lines.append(f"  snippet: {_trunc(c.snippet)}")
        if c.parent_heading:
            lines.append(f"  heading: {_trunc(c.parent_heading)}")
        src = pc.get(c.source_url_key or "", {})
        source_title = src.get("title", "")
        if source_title:
            lines.append(f"  source page: {_build_source_line(src, str(source_title))}")
        lines.append(f"  depth: {c.depth}")
    return "\n".join(lines)


def _summarize_page(entry: dict[str, Any]) -> str:
    """One line per prior relevant page, from whatever fields exist."""
    for key in ("title", "url", "summary"):
        value = entry.get(key)
        if value:
            return _trunc(str(value))
    return _trunc(str(entry))


#: How much of the source page's summary reaches the prompt.  Kept short
#: on purpose: the verdict carries most of the signal and a full summary
#: per candidate would bloat a 30-candidate batch for little gain.
_SUMMARY_CHARS = 60


def _build_source_line(src: dict[str, Any], title: str) -> str:
    """Describe the source page, with the analyzer's verdict when known.

    The verdict is what lets the model tell a link off a RELEVANT article
    from a link off a help page.  A page that has not been analyzed yet
    yields the bare title, which is byte-for-byte the pre-2.9 output.
    """
    line = _trunc(title)
    classification = str(src.get("classification", ""))
    if not classification:
        return line
    line += f" [{classification} {float(src.get('relevance', 0.0)):.2f}]"
    summary = str(src.get("summary", "")).strip()
    if summary:
        line += f" — {_trunc(summary, _SUMMARY_CHARS)}"
    return line


def _trunc(text: str, limit: int = _MAX_FIELD_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _parse_response(content: str) -> dict[str, Any] | None:
    """Parse the model's JSON with the shared tolerant parser."""
    return parse_json_response(content)


def _to_decisions(
    candidates: list[Candidate],
    data: dict[str, Any],
    *,
    tokens_used: int,
    now: datetime.datetime,
    demote_dropped: bool = False,
) -> list[RankDecision]:
    """Turn the parsed response into one decision per candidate.

    Candidates in rankings are kept with the model's priority (clamped
    to [0, 1]); candidates in candidates_to_drop are dropped; ids the
    model did not mention are kept with a neutral priority.

    Under *demote_dropped* a rejection becomes the lowest priority there
    is instead of a removal. What the model would have discarded is then
    read last and only if the page budget reaches it, so the run's own
    limit decides where to stop rather than one model's yes or no. It
    costs a fetch for everything the model doubted, which is the point:
    a wrong keep is a page you skim, a wrong drop is a result you never
    learn existed.
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

    # A rejection carries its reason, so a mistaken one can be read back
    # rather than guessed at.  Bare ids stay valid: the older shape, and
    # what a model returns when it ignores the instruction.
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
            # The tag stays in front of the reason: it is what marks the
            # decision as a rejection for anything counting them later,
            # and the reason is what makes a mistaken one readable.
            tag = _DEMOTED_TAG if demote_dropped else _DROP_TAG
            why = drops[cid]
            rationale = f"{tag}: {why}" if why else tag
            priority, dropped = (_DEMOTED_PRIORITY, False) if demote_dropped else (0.0, True)
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
