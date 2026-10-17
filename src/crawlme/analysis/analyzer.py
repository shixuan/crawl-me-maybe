"""Classify pages and extract fields with source evidence.

Return supported results or raise an error for the caller to handle."""

from __future__ import annotations

import json
import logging
import unicodedata
from typing import Any, cast

from crawlme import prompts
from crawlme.analysis.models import AnalysisResult, AnalyzerFeedback, Classification, ExtractedField, ResultItem
from crawlme.config import Settings
from crawlme.digest import Page
from crawlme.errors import Error, Kind
from crawlme.llm import Client, Stage, TokenBudget, parse_json_response
from crawlme.logging import where
from crawlme.logging.progress import activity
from crawlme.runtime import Goal, spec_fields, spec_time_field, spec_version
from crawlme.util.dates import read_range

logger = logging.getLogger(__name__)


class AnalysisError(Error):
    source = "analysis"
    code = "invalid_response"
    kind = Kind.INVALID_OUTPUT


# Default analyzer text limit; Settings can override it.
_MAX_PAGE_CHARS = 3000
_MAX_TAGS = 8

_VALID_CLASSIFICATIONS = frozenset(Classification.__args__)  # type: ignore[attr-defined]


class Analyzer:
    """Extract supported results from a page."""

    def __init__(self, client: Client, *, max_page_chars: int = _MAX_PAGE_CHARS) -> None:
        self._client = client
        self._max_page_chars = max_page_chars

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> Analyzer | None:
        """Build from settings with the shared budget, or return None without LLM configuration."""
        client = Client.from_settings_if_configured(
            settings,
            budget=budget,
            reasoning_effort=settings.llm_analyze_reasoning_effort,
            stage=Stage.ANALYSIS,
        )
        if client is None:
            return None
        return cls(client, max_page_chars=settings.analyzer_max_chars)

    @activity("analyze")
    async def analyze(self, page: Page, goal: Goal) -> AnalysisResult | None:
        text = _page_text(page)
        if not text:
            return None
        prompt = prompts.analysis_input(goal, page, text, self._max_page_chars)
        resp = await self._client.chat(prompt, system=prompts.analysis_system(goal), json_mode=True)
        if resp.truncated:
            raise AnalysisError(
                f"analysis response truncated for {page.url_key}; incomplete items rejected",
                code="response_truncated",
                details={"url_key": page.url_key},
            )
        data = parse_json_response(resp.content)
        if data is None:
            raise AnalysisError(
                f"unparseable JSON for {page.url_key}", code="invalid_json", details={"url_key": page.url_key}
            )
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
        self, data: dict[str, Any], page: Page, goal: Goal, prompt: str
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
                        raise AnalysisError("time correction must return a time object", code="invalid_dates")
                    remaining: list[str] = []
                    _policy_dates(corrected, page, issues=remaining)
                    if remaining:
                        raise AnalysisError(
                            "; ".join(remaining), code="invalid_dates", details={"url_key": page.url_key}
                        )
                    data = {**data, "time": corrected["time"]}
                except AnalysisError as exc:
                    logger.warning("analysis.time_failed url_key=%s error=%s", page.url_key, exc)
        return data, tokens


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


def _parse_extracted(data: dict[str, Any], page: Page, goal: Goal) -> dict[str, ExtractedField]:
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
        raise AnalysisError("items must be an array of objects", code="invalid_items")
    if data.get("classification") == "RELEVANT" and not entries:
        raise AnalysisError("a relevant page must contain at least one supported item", code="invalid_items")
    if data.get("classification") == "IRRELEVANT" and entries:
        raise AnalysisError("an irrelevant page cannot contain result items", code="invalid_items")
    return entries


def _parse_item(data: dict[str, Any], page: Page, goal: Goal) -> ResultItem:
    summary = data.get("summary")
    evidence = data.get("evidence")
    if not isinstance(summary, str) or not summary.strip():
        raise AnalysisError("each item requires a summary", code="missing_summary", details={"url_key": page.url_key})
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(
            not isinstance(quote, str) or not quote.strip() or _normalize(quote) not in _normalize(_page_text(page))
            for quote in evidence
        )
    ):
        raise AnalysisError(
            "each item requires nonempty verbatim source evidence",
            code="unsupported_evidence",
            details={"url_key": page.url_key},
        )
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
    goal: Goal,
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


def _dates_from(extracted: dict[str, ExtractedField], page: Page, goal: Goal) -> dict[str, Any]:
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
