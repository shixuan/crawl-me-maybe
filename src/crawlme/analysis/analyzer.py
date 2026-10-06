"""Classify pages and extract fields with source evidence.

Successful analyses are published through a sink. Failed calls enter a bounded
retry queue; relevant-page summaries feed subsequent ranking."""

from __future__ import annotations

import asyncio
import json
import logging
import unicodedata
from collections.abc import Callable
from typing import Any, Protocol, cast

from crawlme import prompts
from crawlme.config import Settings
from crawlme.llm import LLMClient, LLMError, Stage, TokenBudget, TokenBudgetError, parse_json_response
from crawlme.logging import where
from crawlme.logging.progress import activity
from crawlme.schemas import (
    AnalysisResult,
    AnalyzerFeedback,
    Classification,
    CrawlGoal,
    ExtractedField,
    Page,
    spec_fields,
    spec_time_field,
    spec_version,
)
from crawlme.schemas.analysis import ResultItem
from crawlme.util.dates import read_range

logger = logging.getLogger(__name__)

# Default analyzer text limit; Settings can override it.
_MAX_PAGE_CHARS = 3000
# A page gets at most this many attempts, spaced by a fixed delay.
_MAX_ATTEMPTS = 3
_RETRY_DELAY_SEC = 30.0
_MAX_TAGS = 8

_VALID_CLASSIFICATIONS = frozenset(Classification.__args__)  # type: ignore[attr-defined]


class Analyzer(Protocol):
    """Contract for the page-analysis stage (see PageAnalyzer)."""

    def bind_sink(self, sink: Callable[[AnalysisResult], None]) -> None: ...

    async def analyze(self, page: Page, goal: CrawlGoal) -> AnalysisResult | None: ...

    async def drain_pending(self) -> None: ...
    async def pause(self) -> None: ...
    def resume(self) -> None: ...

    async def aclose(self) -> None: ...


class PageAnalyzer:
    """Analyze pages with bounded retries and publish results through the bound sink."""

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
        # Count queued and active retries so drain_pending() waits for both.
        self._parked_count = 0
        self._paused = False

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> PageAnalyzer | None:
        """Build from settings with the shared budget, or return None without LLM configuration."""
        client = LLMClient.from_settings_if_configured(
            settings,
            budget=budget,
            reasoning_effort=settings.llm_analyze_reasoning_effort,
            stage=Stage.ANALYSIS,
        )
        if client is None:
            return None
        return cls(client, max_page_chars=settings.analyzer_max_chars)

    def bind_sink(self, sink: Callable[[AnalysisResult], None]) -> None:
        """Attach the persistence callback.  Every successful analysis
        is handed to it, whether it succeeded on the first try or on a
        background retry."""
        self._sink = sink

    async def analyze(self, page: Page, goal: CrawlGoal) -> AnalysisResult | None:
        """Analyze a page; publish success or queue a failed call for bounded retries."""
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

    async def pause(self) -> None:
        """Freeze retries, returning an interrupted attempt to the queue."""
        self._paused = True
        if self._drain_task is not None:
            task = self._drain_task
            self._drain_task = None
            task.cancel()
            results = await asyncio.gather(task, return_exceptions=True)
            if isinstance(results[0], Exception):
                raise results[0]

    def resume(self) -> None:
        self._paused = False
        if self._parked_count and self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain())

    async def aclose(self) -> None:
        """Cancel retries and release queued page bodies."""
        try:
            await self.pause()
        finally:
            while not self._pending.empty():
                self._pending.get_nowait()
            self._parked_count = 0

    async def drain_pending(self) -> None:
        """Wait for retries and surface a failed retry worker."""
        while self._parked_count > 0:
            if self._drain_task is None:
                return
            if self._drain_task.done():
                await self._drain_task
                return
            await asyncio.sleep(0.5)

    async def _drain(self) -> None:
        while True:
            entry = await self._pending.get()
            page, goal, attempts = entry
            try:
                await asyncio.sleep(self._retry_delay)
                try:
                    result = await self._analyze_once(page, goal)
                except LLMError as e:
                    self._requeue_or_giveup(page, goal, attempts=attempts + 1, error=e, parked=True)
                    continue
                self._publish(result)
                self._parked_count -= 1
                logger.debug("analysis.retry_ok url_key=%s attempts=%d", page.url_key, attempts + 1)
            except asyncio.CancelledError:
                self._pending.put_nowait(entry)
                raise

    @activity("analyze")
    async def _analyze_once(self, page: Page, goal: CrawlGoal) -> AnalysisResult:
        text = _page_text(page)
        prompt = prompts.analysis_input(goal, page, text, self._max_page_chars)
        resp = await self._client.chat(prompt, system=prompts.analysis_system(goal), json_mode=True)
        if resp.truncated:
            raise LLMError(f"analysis response truncated for {page.url_key}; incomplete items rejected")
        data = parse_json_response(resp.content)
        if data is None:
            raise LLMError(f"unparseable JSON for {page.url_key}")
        tokens = resp.input_tokens + resp.output_tokens
        entries = _item_entries(data)
        if goal.time_policy and data.get("classification") == "RELEVANT":
            for entry in entries:
                corrected, used = await self._correct_time(entry, page, goal, prompt)
                entry.update(corrected)
                tokens += used
        result = _parse_analysis(data, page, goal, model=resp.model, tokens_used=tokens)
        logger.info(
            "judged %s: %s (%.2f), %d items",
            where(page.url.canonical),
            result.classification,
            result.relevance_score,
            len(result.items or []),
        )
        logger.debug(
            "analysis.ok url_key=%s classification=%s relevance=%.2f model=%s tokens=+%d",
            page.url_key,
            result.classification,
            result.relevance_score,
            result.model,
            tokens,
        )
        return result

    async def _correct_time(
        self, data: dict[str, Any], page: Page, goal: CrawlGoal, prompt: str
    ) -> tuple[dict[str, Any], int]:
        tokens = 0
        if goal.time_policy:
            issues: list[str] = []
            _policy_dates(data, page, issues=issues)
            if issues:
                logger.warning("analysis.time_retry url_key=%s errors=%s", page.url_key, "; ".join(issues))
                correction = (
                    prompt
                    + "\n## Time correction\nPrevious time output:\n"
                    + json.dumps(data, ensure_ascii=False)
                    + "\nValidation errors:\n"
                    + "\n".join(issues)
                    + '\nReturn only {"time": {...}} with corrected date evidence. '
                    "Use multiple verbatim quotes when the date and its role occur separately. "
                    "Omit endpoints the page does not establish."
                    " Correct only this item's time; do not borrow dates from other items on the page."
                )
                try:
                    fixed = await self._client.chat(
                        correction,
                        system=prompts.analysis_system(goal)
                        + ' For this correction return only {"time": {...}}; omit all other analysis fields. '
                        + 'If no endpoint is supported, return {"time": {}}.',
                        json_mode=True,
                    )
                    tokens += fixed.input_tokens + fixed.output_tokens
                    corrected = parse_json_response(fixed.content)
                    if corrected is None or not isinstance(corrected.get("time"), dict):
                        raise LLMError("time correction must return a time object")
                    remaining: list[str] = []
                    _policy_dates(corrected, page, issues=remaining)
                    if remaining:
                        raise LLMError("; ".join(remaining))
                    data = {**data, "time": corrected["time"]}
                except LLMError as exc:
                    logger.warning("analysis.time_failed url_key=%s error=%s", page.url_key, exc)
        return data, tokens

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
        if self._drain_task is None and not self._paused:
            self._drain_task = asyncio.create_task(self._drain())


def _page_text(page: Page) -> str:
    return (page.plain_text or "").strip() or (page.markdown or "").strip()


# Reject bare negations, but preserve values such as "no-sugar option".
_NEGATIONS = frozenset(
    {
        "no",
        "none",
        "not",
        "false",
        "n/a",
        "na",
        "nil",
        "null",
        "unknown",
        "unnamed",
        "unspecified",
        "not specified",
        "not mentioned",
        "not stated",
        "not applicable",
        "not limited",
        "not a limited edition",
        "否",
        "无",
        "没有",
        "不是",
        "未知",
        "未说明",
        "未提及",
    }
)


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def _parse_extracted(data: dict[str, Any], page: Page, goal: CrawlGoal) -> dict[str, ExtractedField]:
    """Keep declared, nonempty fields whose evidence appears in normalized page text."""
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
            logger.debug("analysis.evidence_not_found url_key=%s field=%s", page.url_key, name)
            continue
        if _normalize(value) in _NEGATIONS:
            # Bare negations do not establish a field value; omit them.
            logger.debug("analysis.negative_claim url_key=%s field=%s", page.url_key, name)
            continue
        out[name] = ExtractedField(value=value, evidence=evidence)
    return out


def _item_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    if "items" not in data:
        return [data]  # Accept historical single-result responses.
    entries = data["items"]
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise LLMError("items must be an array of objects")
    if data.get("classification") == "RELEVANT" and not entries:
        raise LLMError("a relevant page must contain at least one supported item")
    if data.get("classification") == "IRRELEVANT" and entries:
        raise LLMError("an irrelevant page cannot contain result items")
    return entries


def _parse_item(data: dict[str, Any], page: Page, goal: CrawlGoal) -> ResultItem:
    summary = data.get("summary")
    evidence = data.get("evidence")
    if not isinstance(summary, str) or not summary.strip():
        raise LLMError("each item requires a summary")
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(
            not isinstance(quote, str) or not quote.strip() or _normalize(quote) not in _normalize(_page_text(page))
            for quote in evidence
        )
    ):
        raise LLMError("each item requires nonempty verbatim source evidence")
    extracted = _parse_extracted(data, page, goal)
    dates = _policy_dates(data, page) if goal.time_policy else _dates_from(extracted, page, goal)
    return ResultItem(
        summary=summary.strip(),
        relevance_score=_clamp01(data.get("relevance_score")),
        evidence=evidence,
        extracted=extracted,
        tags=_str_list(data.get("tags"), _MAX_TAGS),
        **dates,
    )


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
    summary = data.get("summary")
    summary = str(summary).strip() if isinstance(summary, str) else ""

    tags = _str_list(data.get("tags"), _MAX_TAGS)
    extracted = _parse_extracted(data, page, goal)
    entries = _item_entries(data)
    items = [_parse_item(entry, page, goal) for entry in entries] if "items" in data else None

    return AnalysisResult(
        page_id=page.page_id,
        url_key=page.url_key,
        goal_id=goal.goal_id,
        classification=cast(Classification, classification),
        relevance_score=relevance,
        summary=summary,
        structured_data=data,
        items=items,
        extracted=extracted,
        spec_version=spec_version(goal.extraction_spec, goal.time_policy),
        **(
            _policy_dates(data, page)
            if goal.time_policy and classification == "RELEVANT"
            else _dates_from(extracted, page, goal)
            if not goal.time_policy
            else {}
        ),
        tags=tags,
        feedback=AnalyzerFeedback(
            classification=classification,
            relevance_score=relevance,
            domain=page.url.reg_domain,
            url=page.url.canonical,
            title=page.title or "",
        ),
        model=model,
        tokens_used=tokens_used,
    )


def _policy_dates(data: dict[str, Any], page: Page, *, issues: list[str] | None = None) -> dict[str, Any]:
    errors = issues if issues is not None else []
    raw = data.get("time")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append("time must be an object")
        return {}
    dates = {}
    text = _normalize(unicodedata.normalize("NFKC", _page_text(page)))
    for endpoint in ("starts_on", "ends_on"):
        entry = raw.get(endpoint)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            errors.append(f"{endpoint}: expected a date value and evidence")
            continue
        value, evidence = entry.get("value"), entry.get("evidence")
        quotes = [evidence] if isinstance(evidence, str) else evidence
        if not isinstance(value, str) or not value.strip() or not isinstance(quotes, list) or not quotes:
            errors.append(f"{endpoint}: provide a date value and nonempty evidence quotes")
            continue
        if any(not isinstance(q, str) or not q.strip() for q in quotes):
            errors.append(f"{endpoint}: evidence quotes must be nonempty strings")
            continue
        if any(_normalize(unicodedata.normalize("NFKC", q)) not in text for q in quotes):
            errors.append(f"{endpoint}: each evidence quote must occur in the page")
            continue
        found = read_range(value, kind="on", said_on=page.published_at)
        supported = [read_range(q, kind="on", said_on=page.published_at) for q in quotes]
        date = (found.start if endpoint == "starts_on" else found.end) if found else None
        if date is None or not any(span and date in (span.start, span.end) for span in supported):
            errors.append(f"{endpoint}: evidence must include the stated date, including compressed ranges")
            continue
        dates[endpoint] = date
    start, end = dates.get("starts_on"), dates.get("ends_on")
    if start is not None and end is not None and start > end:
        errors.append("time: starts_on must not be after ends_on")
        return {}
    return dates


def _dates_from(extracted: dict[str, ExtractedField], page: Page, goal: CrawlGoal) -> dict[str, Any]:
    """Derive event dates from the validated field declared by the goal."""
    declared = spec_time_field(goal.extraction_spec)
    if declared is None:
        return {}
    name, kind = declared
    field = extracted.get(name)
    if field is None:
        return {}
    found = read_range(field.value, kind=kind, said_on=page.published_at)
    if found is None:
        return {}
    return {
        "starts_on": found.start,
        "ends_on": found.end,
    }


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
