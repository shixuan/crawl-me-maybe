"""PageAnalyzer: one LLM call per fetched page, the v0.2 analysis stage.

The analyzer is its own stage: the judgment it produces (classification,
relevance, summary) is the product the user consumes, stored in the
analyses table and revisited by replay and the benchmark judge.  The
same call also yields the feedback signals (hub quality, endorsed
links, topics, entities) that the steering half of the feedback loop
turns into domain priors and priority multipliers, and the scheduler
later feeds endorsed links back into the candidate buffer.

The whole feedback subsystem, analyzer included, is optional: the
factory only wires it when enabled and credentialed.

Failure policy.  A failed analysis never blocks the crawl loop: the
page is parked on an internal delayed re-analysis queue and retried a
bounded number of times in the background.  Every successful analysis
(both first try and retry) is published through the sink bound at
construction time, which is how the AnalysisResult row gets persisted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Protocol, cast

from crawlme.config import Settings
from crawlme.llm import LLMClient, LLMError, TokenBudget, TokenBudgetError, parse_json_response
from crawlme.schemas import (
    AnalysisResult,
    AnalyzerFeedback,
    Classification,
    CrawlGoal,
    ExtractedField,
    Page,
    spec_fields,
    spec_version,
)

logger = logging.getLogger(__name__)

# Response cap: a summary plus five short lists fit comfortably.
# Reasoning models spend this before writing any JSON, and asking for
# extracted fields makes them think harder.  Only generated tokens are
# billed, so the headroom is free until it is needed.
_MAX_TOKENS = 4096
# Default page-text cap sent to the model.  The 10-replicate
# benchmark (benchmark/feedback/) picked 3000: the 6000-char window
# misclassified long-form pages, while 3000 hits the intro zone and
# wins on both precision and recall.  The knob lives in
# Settings.analyzer_max_chars.
_MAX_PAGE_CHARS = 3000
# A page gets at most this many attempts, spaced by a fixed delay.
_MAX_ATTEMPTS = 3
_RETRY_DELAY_SEC = 30.0
# Bump when the prompt changes in a way that changes outputs, so
# stored analyses stay comparable across versions.
_PROMPT_VERSION = "v2.5"

_MAX_TAGS = 8
_MAX_TOPICS = 10
_MAX_ENTITIES = 10
_MAX_ENDORSED = 5

_VALID_CLASSIFICATIONS = frozenset(Classification.__args__)  # type: ignore[attr-defined]

_SYSTEM = (
    "You analyze web pages for a goal-directed crawler. You get the crawl goal, the page "
    "URL, title, and text. Classify the page, summarize what it offers, and produce "
    "feedback signals the crawler's scheduler uses. Reply with JSON only, no prose. "
    'Format: {"classification": "<RELEVANT|HUB|AGGREGATOR|IRRELEVANT|NAVIGATION>", '
    '"relevance_score": 0.0, "hub_score": 0.0, "summary": "...", "tags": ["..."], '
    '"topics": ["..."], "entities": ["..."], "endorsed_links": ["..."]}. '
    "classification: RELEVANT means the page directly satisfies the goal; HUB means the "
    "page itself is thin but links toward the goal; AGGREGATOR means a link aggregator "
    "like a Hacker News front page; IRRELEVANT means unrelated; NAVIGATION means menus, "
    "login pages, category indexes. relevance_score is how well the page satisfies the "
    "goal, hub_score is how good this page is as a link source for the goal, both 0.0 to "
    "1.0. summary is one or two sentences. tags describe the content. topics are the "
    "subjects the page is about. entities are named things (projects, people, companies) "
    "that matter. endorsed_links are up to 5 URLs from the page text that you would "
    "click yourself."
)


_EXTRACT_SYSTEM = (
    ' Also fill "extracted": {"<field>": {"value": "...", "evidence": "..."}} for the '
    "fields listed under ## Extract. evidence must be copied verbatim from the page "
    "text and must contain the value. Omit any field the page does not state: a field "
    "you leave out is read as unknown, and that is the correct answer whenever the page "
    "does not say. Never infer a value from what is likely, and never use the goal's "
    "own wording as evidence."
)


class Analyzer(Protocol):
    """Contract for the page-analysis stage (see PageAnalyzer)."""

    def bind_sink(self, sink: Callable[[AnalysisResult], None]) -> None: ...

    async def analyze(self, page: Page, goal: CrawlGoal) -> AnalysisResult | None: ...

    async def drain_pending(self) -> None: ...

    async def aclose(self) -> None: ...


class PageAnalyzer:
    """Classifies and summarizes fetched pages with one LLM call each.

    Failed analyses are parked on an internal delayed re-analysis
    queue: analyze() returns None immediately and a background task
    retries a bounded number of times, so the caller's loop never
    waits on the LLM.  Every success (first try or retry) is handed to
    the bound sink.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        max_attempts: int = _MAX_ATTEMPTS,
        retry_delay: float = _RETRY_DELAY_SEC,
        max_page_chars: int = _MAX_PAGE_CHARS,
    ) -> None:
        self._client = client
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay
        self._max_page_chars = max_page_chars
        self._sink: Callable[[AnalysisResult], None] | None = None
        self._pending: asyncio.Queue[tuple[Page, CrawlGoal, int]] = asyncio.Queue()
        self._drain_task: asyncio.Task[None] | None = None
        # Parked pages not yet settled: items in the queue plus the one
        # the drain task currently holds between retries.  drain_pending()
        # waits on it.
        self._parked_count = 0

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> PageAnalyzer | None:
        """Default-on with graceful auto-off, mirroring the other LLM
        stages: without credentials there is nothing to call.  *budget*
        is shared across all LLM consumers of the task."""
        client = LLMClient.from_settings_if_configured(settings, budget=budget)
        if client is None:
            return None
        return cls(client, max_page_chars=settings.analyzer_max_chars)

    def bind_sink(self, sink: Callable[[AnalysisResult], None]) -> None:
        """Attach the persistence callback.  Every successful analysis
        is handed to it, whether it succeeded on the first try or on a
        background retry."""
        self._sink = sink

    async def analyze(self, page: Page, goal: CrawlGoal) -> AnalysisResult | None:
        """One analysis attempt.  On failure the page is parked for a
        delayed background retry and None is returned, so the crawl
        loop never blocks on the LLM."""
        if not _page_text(page):
            logger.debug("analysis.skip_empty url_key=%s", page.url_key)
            return None
        try:
            result = await self._analyze_once(page, goal)
        except LLMError as e:
            self._requeue_or_giveup(page, goal, attempts=1, error=e)
            return None
        self._publish(result)
        return result

    async def aclose(self) -> None:
        """Cancel the background retry loop, dropping parked pages."""
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None

    async def drain_pending(self) -> None:
        """Wait until every parked page is settled (success or giveup).

        The crawl loop never needs this: parked pages retry in the
        background and the crawl moves on.  Batch consumers like replay
        must wait, otherwise aclose() would cancel the drain and drop
        pages mid-retry.
        """
        while self._parked_count > 0:
            if self._drain_task is None or self._drain_task.done():
                # The drain died on an unexpected error; nothing will
                # settle these pages.  Stop waiting instead of hanging.
                logger.warning("analysis.drain_dead pending_dropped=%d", self._parked_count)
                self._parked_count = 0
                break
            await asyncio.sleep(0.5)

    async def _drain(self) -> None:
        """Background retries: wait the delay, try again, repeat."""
        while True:
            page, goal, attempts = await self._pending.get()
            await asyncio.sleep(self._retry_delay)
            try:
                result = await self._analyze_once(page, goal)
            except LLMError as e:
                self._requeue_or_giveup(page, goal, attempts=attempts + 1, error=e, parked=True)
                continue
            self._publish(result)
            # Settled: this parked page is done either way now.
            self._parked_count -= 1
            logger.info("analysis.retry_ok url_key=%s attempts=%d", page.url_key, attempts + 1)

    async def _analyze_once(self, page: Page, goal: CrawlGoal) -> AnalysisResult:
        text = _page_text(page)
        prompt = _build_prompt(goal, page, text, self._max_page_chars)
        resp = await self._client.chat(prompt, system=_system_for(goal), max_tokens=_MAX_TOKENS, json_mode=True)
        data = parse_json_response(resp.content)
        if data is None:
            raise LLMError(f"unparseable JSON for {page.url_key}")
        tokens = resp.input_tokens + resp.output_tokens
        result = _parse_analysis(data, page, goal, model=resp.model, tokens_used=tokens)
        logger.info(
            "analysis.ok url_key=%s classification=%s relevance=%.2f hub=%.2f model=%s tokens=+%d",
            page.url_key,
            result.classification,
            result.relevance_score,
            result.feedback.hub_score,
            result.model,
            tokens,
        )
        return result

    def _publish(self, result: AnalysisResult) -> None:
        if self._sink is not None:
            self._sink(result)

    def _requeue_or_giveup(
        self, page: Page, goal: CrawlGoal, *, attempts: int, error: LLMError, parked: bool = False
    ) -> None:
        # An exhausted token budget never recovers within this task, so
        # don't park pages behind it.
        if isinstance(error, TokenBudgetError) or attempts >= self._max_attempts:
            if parked:
                # The drain held this page between retries; it is now
                # settled, so release the count drain_pending() waits on.
                self._parked_count -= 1
            logger.warning("analysis.giveup url_key=%s attempts=%d error=%s", page.url_key, attempts, error)
            return
        logger.warning("analysis.requeue url_key=%s attempts=%d error=%s", page.url_key, attempts, error)
        self._pending.put_nowait((page, goal, attempts))
        # A fresh parking counts once; a re-parking from the drain was
        # already counted (the drain holds the count while it retries).
        if not parked:
            self._parked_count += 1
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain())


def _page_text(page: Page) -> str:
    return (page.plain_text or "").strip() or (page.markdown or "").strip()


def _system_for(goal: CrawlGoal) -> str:
    """The contract, widened when the goal declares fields to collect."""
    return _SYSTEM + _EXTRACT_SYSTEM if spec_fields(goal.extraction_spec) else _SYSTEM


def _build_prompt(goal: CrawlGoal, page: Page, text: str, max_chars: int) -> str:
    """Assemble the user prompt: goal, fields to collect, page, text."""
    lines = ["## Goal", goal.goal_statement or goal.prompt]
    fields = spec_fields(goal.extraction_spec)
    if fields:
        lines.append("## Extract")
        lines.extend(f"- {name}: {desc}" for name, desc in fields.items())
    lines.extend(["## Page", page.url.canonical])
    if page.title:
        lines.append(f"Title: {page.title}")
    lines.append("")
    lines.append(text[:max_chars])
    return "\n".join(lines)


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def _parse_extracted(data: dict[str, Any], page: Page, goal: CrawlGoal) -> dict[str, ExtractedField]:
    """Keep the declared fields whose evidence is really in the page.

    The check is what makes a result something to act on rather than
    something to trust.  A model that paraphrases the page, or quotes the
    goal back, produces evidence that is not there, and the field is
    dropped instead of stored.
    """
    fields = spec_fields(goal.extraction_spec)
    if not fields:
        return {}
    raw = data.get("extracted")
    if not isinstance(raw, dict):
        return {}
    haystack = _normalize(page.plain_text or "")
    out: dict[str, ExtractedField] = {}
    for name in fields:
        entry = raw.get(name)
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("value", "")).strip()
        evidence = str(entry.get("evidence", "")).strip()
        if not value or not evidence:
            continue
        if _normalize(evidence) not in haystack:
            logger.info("analysis.evidence_not_found url_key=%s field=%s", page.url_key, name)
            continue
        out[name] = ExtractedField(value=value, evidence=evidence)
    return out


def _parse_analysis(
    data: dict[str, Any],
    page: Page,
    goal: CrawlGoal,
    *,
    model: str,
    tokens_used: int,
) -> AnalysisResult:
    """Turn the parsed response into a validated AnalysisResult.

    Unknown classifications degrade to UNKNOWN, scores are clamped to
    [0, 1], and every list is deduplicated and capped.
    """
    raw_classification = data.get("classification")
    classification = raw_classification.upper() if isinstance(raw_classification, str) else ""
    if classification not in _VALID_CLASSIFICATIONS:
        classification = "UNKNOWN"

    relevance = _clamp01(data.get("relevance_score"))
    hub = _clamp01(data.get("hub_score"))
    summary = data.get("summary")
    summary = str(summary).strip() if isinstance(summary, str) else ""

    tags = _str_list(data.get("tags"), _MAX_TAGS)
    topics = _str_list(data.get("topics"), _MAX_TOPICS)
    entities = _str_list(data.get("entities"), _MAX_ENTITIES)
    endorsed = _str_list(data.get("endorsed_links"), _MAX_ENDORSED)

    return AnalysisResult(
        page_id=page.page_id,
        url_key=page.url_key,
        goal_id=goal.goal_id,
        classification=cast(Classification, classification),
        relevance_score=relevance,
        summary=summary,
        structured_data=data,
        extracted=_parse_extracted(data, page, goal),
        spec_version=spec_version(goal.extraction_spec),
        tags=tags,
        feedback=AnalyzerFeedback(
            classification=classification,
            relevance_score=relevance,
            hub_score=hub,
            endorsed_links=endorsed,
            topics=topics,
            entities=entities,
            domain=page.url.reg_domain,
            url=page.url.canonical,
            title=page.title or "",
        ),
        model=model,
        prompt_version=_PROMPT_VERSION,
        tokens_used=tokens_used,
    )


def _clamp01(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _str_list(value: object, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            s = item.strip()
            if s and s not in out:
                out.append(s)
        if len(out) >= limit:
            break
    return out
