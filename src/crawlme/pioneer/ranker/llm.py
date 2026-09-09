"""LLMRanker: batched LLM fine-ranking, the final funnel stage (v0.2).

RuleRanker is the relaxed pre-filter; LLMRanker decides.  Each batch of
survivors (at most _BATCH_SIZE per call) is sent to the LLM in a single
request.  The model sees the goal, what the crawl found so far, and the
whole batch at once, so it compares links against each other instead of
judging each in isolation.  The response carries a priority and
rationale per candidate and a drop list for the ones that would not
answer the goal.

Failure policy.  An LLMError (provider failure, token budget
exhausted) propagates.  Nothing catches it any more, since the stages
that used to stand behind this one are gone, so the scheduler reads a
dead rank pump as fatal and ends the run saying why.  An unparseable
JSON response gets one repair retry with a stricter instruction; if
that also fails, the batch fails the same way.

Partial responses are tolerated fail-open.  Candidates the model did
not mention in either list are kept with a neutral priority, because
the house rule is to over-crawl rather than lose good links.
"""

from __future__ import annotations

import datetime
import logging
import math
from typing import Any

from crawlme.config import Settings
from crawlme.llm import LLMClient, LLMError, Stage, TokenBudget, parse_json_response
from crawlme.logging import where
from crawlme.schemas import Candidate, CrawlGoal, RankDecision, RankHistorySummary, spec_fields

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
# Below this a candidate is refused. Zero refuses nothing, because a
# score is never negative, which is also what --recall means.
_KEEP_EVERYTHING = 0.0
# How far a failed condition is held away from zero, so that failing one
# and failing three stay different numbers.
_CONDITION_FLOOR = 0.01
# At most this many previously-relevant pages are shown to the model.
_MAX_RELEVANT = 5

# Judged one candidate at a time, against the goal's own conditions.
#
# The old contract asked the model to compare the batch against itself,
# which made a score mean "better than these nineteen" rather than
# anything fixed. One measured batch scored every candidate at 0.50 or
# above and produced 2 relevant pages out of 20, while a batch with a
# similar mean produced 6 out of 7. A comparison cannot be thresholded
# and cannot be read across runs; a judgement about one page can.
#
# The conditions come from the goal enhancer, so the factors change with
# the goal and this module never names one.
_SYSTEM_HEAD = (
    "You decide which links a web crawler should fetch under a limited budget, and in "
    "what order. You see a batch of candidate links plus the crawl goal and what the "
    "crawl found so far. "
    "Judge each candidate on its own against the goal, not against the other "
    "candidates in the batch: the same text must get the same scores whichever batch "
    "it arrives in. Reply with JSON only, no prose."
)

_SYSTEM_TAIL = (
    " Score only from what the candidate itself shows. A candidate that shows too "
    "little to tell scores in the middle, never at either end: the low end is for "
    "what you can see does not answer, and being unsure is not that."
)


def _system_for(goal: CrawlGoal) -> str:
    """The contract, named after the goal's own conditions."""
    conds = goal.constraints or {}
    if conds:
        keys = ", ".join(f'"{k}": 0.0' for k in conds)
        body = (
            f' Format: {{"rankings": [{{"id": "<id>", "match": 0.0, {keys}}}]}}. '
            "match is how well this candidate answers the goal overall, 0.0 to 1.0. "
            "Each remaining field scores how far the candidate satisfies that one "
            "condition, listed under ## Conditions, also 0.0 to 1.0."
        )
    else:
        body = (
            ' Format: {"rankings": [{"id": "<id>", "match": 0.0}]}. '
            "match is how well this candidate answers the goal, 0.0 to 1.0."
        )
    return _SYSTEM_HEAD + body + " Include every candidate id exactly once." + _SYSTEM_TAIL


_REPAIR_SUFFIX = (
    "\n\nYour previous answer was not valid JSON. Reply with JSON only, no prose, in the exact format requested."
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class LLMRanker:
    """Fine-ranks batches of candidates with one LLM call per batch.

    On provider failure the exception propagates and the scheduler ends
    the run with it. Nothing scores candidates behind this, so carrying
    on would crawl in frontier order and report a normal finish.
    """

    def __init__(
        self,
        client: LLMClient,
        batch_size: int = _BATCH_SIZE,
        threshold: float = _KEEP_EVERYTHING,
        max_batch_chars: int = _MAX_BATCH_CHARS,
    ) -> None:
        self._client = client
        self._batch_size = batch_size
        self._threshold = threshold
        self._max_batch_chars = max_batch_chars
        # Lowered whenever a reply runs out of room, so the cost of
        # learning the right size is paid once rather than per batch.
        self._cap = batch_size

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> LLMRanker | None:
        """Default-on with graceful auto-off: without credentials there
        is nothing to call, so the stage is skipped entirely.  *budget*
        is shared across all LLM consumers of the task."""
        # The ranking stage takes its own reasoning setting when one is
        # given: it orders candidates for fetching, and the analyzer
        # judges every page again afterwards, so the trade here is not
        # the trade the analyzer faces.
        client = LLMClient.from_settings_if_configured(
            settings,
            budget=budget,
            reasoning_effort=settings.llm_rank_reasoning_effort,
            stage=Stage.RANKING,
        )
        if client is None:
            return None
        # --recall is the threshold at zero: the diagnostic mode exists
        # to read the rejects, so it must not create any.
        threshold = _KEEP_EVERYTHING if settings.recall else settings.rank_threshold
        return cls(client, threshold=threshold, max_batch_chars=settings.llm_max_batch_chars)

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
        prompt = _build_prompt(goal, chunk, history, page_contexts)
        resp = await self._client.chat(prompt, system=_system_for(goal), json_mode=True)
        data = _parse_response(resp.content)
        if data is None:
            # A reply that used the whole ceiling was cut off mid-JSON.
            # Raising the ceiling was the first answer and the wrong one:
            # the reply has to be that long because the batch is that
            # big, so a bigger ceiling buys another slow call that runs
            # out too.  One run spent four of them, 33k wasted output
            # tokens, and 284 seconds -- half its total time -- doubling
            # its way through the same twenty-one candidates.
            #
            # The ceiling belongs to the model; the batch size is ours.
            if resp.truncated and len(chunk) > 1:
                await self._halve_batches(len(chunk))
                mid = len(chunk) // 2
                first = await self._rank_chunk(goal, chunk[:mid], history, page_contexts)
                return first + await self._rank_chunk(goal, chunk[mid:], history, page_contexts)
            if resp.truncated:
                # One candidate that will not fit is the only case where
                # more room is the answer, because there is nothing to
                # split.
                logger.warning("llm.rank one candidate overruns the ceiling, retrying with more room")
                resp = await self._client.chat(
                    prompt, system=_system_for(goal), max_tokens=resp.output_tokens * 2, json_mode=True
                )
            else:
                logger.warning(
                    "llm.rank unparseable json for %d candidates, retrying once with a stricter instruction",
                    len(chunk),
                )
                resp = await self._client.chat(prompt + _REPAIR_SUFFIX, system=_system_for(goal), json_mode=True)
            data = _parse_response(resp.content)
        if data is None:
            raise LLMError(f"unparseable JSON for {len(chunk)} candidates after repair retry")

        tokens = resp.input_tokens + resp.output_tokens
        decisions = _to_decisions(chunk, data, goal=goal, tokens_used=tokens, now=_utcnow(), threshold=self._threshold)
        kept = sum(1 for d in decisions if not d.dropped)
        logger.debug(
            "llm.rank batch=%d kept=%d dropped=%d threshold=%.2f model=%s tokens=+%d",
            len(chunk),
            kept,
            len(decisions) - kept,
            self._threshold,
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
    """Assemble the user prompt: goal, fields to collect, prior findings, candidate batch."""
    lines = ["## Goal", goal.goal_statement or goal.prompt]
    lines.extend(_window_lines(goal))
    lines.extend(_condition_lines(goal))
    lines.extend(_extract_lines(goal))
    if history.relevant_pages:
        # Deduplicated, because identical lines are not five findings.
        # Instagram titles every page "Instagram", so this block once
        # said that word five times and called itself feedback.
        seen: list[str] = []
        for entry in history.relevant_pages[:_MAX_RELEVANT]:
            line = f"- {_summarize_page(entry)}"
            if line not in seen:
                seen.append(line)
        lines.append("## Seen so far")
        lines.extend(seen)
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
        if c.posted_at:
            lines.append(f"  posted: {_age_of(c.posted_at)}")
        src = pc.get(c.source_url_key or "", {})
        source_title = src.get("title", "")
        if source_title:
            lines.append(f"  source page: {_build_source_line(src, str(source_title))}")
        lines.append(f"  depth: {c.depth}")
    return "\n".join(lines)


def _extract_lines(goal: CrawlGoal) -> list[str]:
    """The fields the analyzer will look for, said here too.

    The two stages used to judge different goals, the ranker the user's
    raw wording and the analyzer the enhanced statement plus these
    fields.  A candidate that plainly cannot yield them is one the
    analyzer will reject, and the ranker had no way to know that.
    """
    fields = spec_fields(goal.extraction_spec)
    if not fields:
        return []
    return ["## Extract", *(f"- {name}: {desc}" for name, desc in fields.items())]


def _condition_lines(goal: CrawlGoal) -> list[str]:
    """The goal's own conditions, one per line, named as they are scored.

    Named rather than described in prose because each name is a key in
    the reply, and the score is only readable next to the condition it
    answers.
    """
    conds = goal.constraints or {}
    if not conds:
        return []
    return ["## Conditions"] + [f"- {name}: {text}" for name, text in conds.items()]


def _window_lines(goal: CrawlGoal) -> list[str]:
    """The window actually in force, said out loud.

    The prompt is the user's own words and can disagree with it: asked
    for "this month" with --since "1 week", the model ranked three-week
    old posts highly and the filter had already dropped them.
    """
    if goal.since is None:
        return []
    return ["## Window", f"Anything published before {goal.since:%Y-%m-%d} is out of scope."]


def _age_of(posted_at: datetime.datetime) -> str:
    """How long ago, relative: the model is comparing candidates, not dates."""
    # Naive dates read as UTC, as everywhere else. Subtracting one raises,
    # and that would lose the whole batch over an advisory field.
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=datetime.timezone.utc)
    seconds = (_utcnow() - posted_at).total_seconds()
    if seconds < 0:
        return "just now"
    hours = seconds / 3600
    if hours < 48:
        return f"{hours:.0f}h ago"
    days = hours / 24
    return f"{days:.0f}d ago" if days < 90 else f"{days / 30:.0f}mo ago"


def _summarize_page(entry: dict[str, Any]) -> str:
    """One line per prior relevant page, from whatever fields exist.

    The summary leads because it is what analysis established. The title
    is whatever the page put in its head tag, which on a platform that
    serves one title for every page is a constant, and the fallback
    chain used to reach it first.
    """
    for key in ("summary", "title", "url"):
        value = entry.get(key)
        if value:
            return _trunc(str(value))
    return _trunc(str(entry))


# How much of the source page's summary reaches the prompt.  Kept short
# on purpose: the verdict carries most of the signal and a full summary
# per candidate would bloat a 30-candidate batch for little gain.
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
    goal: CrawlGoal,
    tokens_used: int,
    now: datetime.datetime,
    threshold: float = _KEEP_EVERYTHING,
) -> list[RankDecision]:
    """One decision per candidate, from its own factor scores.

    The model no longer says what to drop. It scores, and *threshold*
    decides, so how much a run refuses is a number it is given rather
    than one model's mood: measured drop rates for the same goal ran
    from 51% to 69% with nobody able to move them.

    A candidate the model did not mention keeps a neutral priority and
    is never refused on silence.
    """
    conds = tuple(goal.constraints or {})
    scored = _parse_scores(data, conds)
    unknown = set(scored) - {c.candidate_id for c in candidates}
    if unknown:
        logger.warning("llm.rank unknown_ids=%s", sorted(unknown))

    missing = 0
    decisions: list[RankDecision] = []
    for c in candidates:
        factors = scored.get(c.candidate_id)
        if factors is None:
            factors = {}
            missing += 1
            priority, dropped = _NEUTRAL_PRIORITY, False
        else:
            priority = _combine(factors, conds)
            dropped = priority < threshold
        rationale = _rationale(factors, conds) or "no_opinion"
        logger.info("scored %.2f %s%s", priority, where(c.url.canonical), _aside(rationale))
        logger.debug(
            "rank.scored url_key=%s priority=%.2f factors=%s dropped=%s",
            c.url.url_key,
            priority,
            rationale,
            dropped,
        )
        decisions.append(
            RankDecision(
                candidate_id=c.candidate_id,
                url_key=c.url.url_key,
                priority=priority,
                factors=factors,
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


def _parse_scores(data: dict[str, Any], conds: tuple[str, ...]) -> dict[str, dict[str, float]]:
    """id -> every factor it was scored on, clamped to [0, 1]."""
    out: dict[str, dict[str, float]] = {}
    raw = data.get("rankings")
    if not isinstance(raw, list):
        return out
    for r in raw:
        if not isinstance(r, dict):
            continue
        cid = r.get("id")
        if not isinstance(cid, str) or not cid:
            continue
        got = {
            k: _clamp01(r[k])
            for k in ("match", *conds)
            if isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)
        }
        if got:
            out[cid] = got
    return out


def _combine(factors: dict[str, float], conds: tuple[str, ...]) -> float:
    """Overall match, discounted by how far the conditions are met.

    The conditions are a conjunction, so their product is what they
    jointly say. Taken raw that product shrinks with the number of
    conditions -- all of them at 0.8 gives 0.64 for two and 0.33 for
    five -- which would make one threshold strict on a detailed goal and
    loose on a broad one. The geometric mean is that product normalised
    by how many there are, so all-at-0.8 is 0.8 whatever the count. It
    is a monotone transform, so nothing is reordered: comparing the
    geometric mean against t is the same decision as comparing the raw
    product against t**k.

    Floored rather than allowed to reach zero. One failed condition
    should sink a candidate, but a candidate that fails one is not the
    same as a candidate that fails three, and at zero they are
    indistinguishable for ever after.
    """
    match = factors.get("match", _NEUTRAL_PRIORITY)
    met = [factors[k] for k in conds if k in factors]
    if not met:
        return round(match, 4)
    logs = sum(math.log(max(c, _CONDITION_FLOOR)) for c in met)
    return round(match * math.exp(logs / len(met)), 4)


def _rationale(factors: dict[str, float], conds: tuple[str, ...]) -> str:
    """The scores as one readable line, so a priority can be argued with."""
    if not factors:
        return ""
    return " ".join(f"{k}={factors[k]:.2f}" for k in ("match", *conds) if k in factors)


def _clamp01(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(value)))
