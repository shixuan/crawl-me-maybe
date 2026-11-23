"""Validate model output against the page and the extraction goal."""

from __future__ import annotations

import datetime
import logging
import unicodedata
from typing import Any, cast

from crawlme.analysis.models import AnalysisResult, AnalyzerFeedback, Classification, ExtractedField, ResultItem
from crawlme.digest import Page
from crawlme.digest.models import Source
from crawlme.errors import Error, Kind
from crawlme.goals import Goal, spec_fields, spec_time_field, spec_version
from crawlme.util.dates import read_range

logger = logging.getLogger(__name__)


class AnalysisError(Error):
    source = "analysis"
    code = "invalid_response"
    kind = Kind.INVALID_OUTPUT


_MAX_TAGS = 8

_VALID_CLASSIFICATIONS = frozenset(Classification.__args__)  # type: ignore[attr-defined]


def page_text(page: Page) -> str:
    return (page.plain_text or "").strip() or (page.markdown or "").strip()


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
        if not isinstance(entry, dict) or entry.get("value") is None:
            continue
        value = str(entry["value"]).strip()
        evidence = str(entry.get("evidence", "")).strip()
        if not value or not evidence:
            continue
        if _normalize(evidence) not in haystack:
            logger.debug("analysis.evidence_not_found url_key=%s field=%s", page.url_key, name)
            continue
        out[name] = ExtractedField(value=value, evidence=evidence)
    return out


def item_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
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
    sources = []
    if page.sources:
        refs = data.get("sources")
        available = {str(i): source for i, source in enumerate(page.sources, 1)}
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) or ref not in available for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise AnalysisError(
                f"item sources must be distinct string refs from {list(available)}; received {refs!r}",
                code="invalid_sources",
                details={"url_key": page.url_key, "summary": summary},
            )
        sources = [available[ref] for ref in refs]
        page = page.model_copy(update={"plain_text": "\n\n".join(source.text for source in sources), "markdown": None})
    elif data.get("sources"):
        raise AnalysisError("source refs were not supplied for this page", code="invalid_sources")
    if not isinstance(summary, str) or not summary.strip():
        raise AnalysisError("each item requires a summary", code="missing_summary", details={"url_key": page.url_key})
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(
            not isinstance(quote, str) or not quote.strip() or _normalize(quote) not in _normalize(page_text(page))
            for quote in evidence
        )
        or (
            sources
            and any(not any(_normalize(quote) in _normalize(source.text) for source in sources) for quote in evidence)
        )
    ):
        raise AnalysisError(
            "each item requires nonempty verbatim source evidence",
            code="unsupported_evidence",
            details={"url_key": page.url_key},
        )
    extracted = _parse_extracted(data, page, goal)
    if any(not any(_normalize(quote) in _normalize(source.text) for quote in evidence) for source in sources):
        raise AnalysisError("each cited source needs supporting item evidence", code="unsupported_evidence")
    dates = validate_time(data, page) if goal.time_policy else _dates_from(extracted, page, goal)
    return ResultItem(
        summary=summary.strip(),
        relevance_score=_clamp01(data.get("relevance_score")),
        evidence=evidence,
        sources=[Source(url=source.url, author=source.author, published_at=source.published_at) for source in sources],
        extracted=extracted,
        tags=_str_list(data.get("tags"), _MAX_TAGS),
        **dates,
    )


def validate_analysis(
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
    entries = item_entries(data)
    if page.sources and classification == "RELEVANT" and "items" not in data:
        raise AnalysisError("sourced pages require items with source refs", code="invalid_items")
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
            validate_time(data, page)
            if goal.time_policy and classification == "RELEVANT" and items is None
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


def validate_time(data: dict[str, Any], page: Page, *, issues: list[str] | None = None) -> dict[str, Any]:
    errors = issues if issues is not None else []

    def invalid(message: str) -> None:
        if issues is None:
            raise AnalysisError(message, code="invalid_dates", details={"url_key": page.url_key})
        errors.append(message)

    raw = data.get("time")
    if not isinstance(raw, dict):
        invalid("time must be an object")
        return {}
    status = raw.get("status")
    if status not in ("extracted", "not_stated", "ambiguous"):
        invalid("time.status must be extracted, not_stated or ambiguous")
        return {}
    endpoints = [key for key in ("starts_on", "ends_on") if raw.get(key) is not None]
    if status != "extracted":
        if endpoints:
            invalid("unknown time must not contain date endpoints")
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            invalid("not_stated or ambiguous time requires a nonempty reason")
        return {}
    if not endpoints:
        invalid("extracted time requires at least one supported endpoint")
        return {}
    dates = {}
    text = _normalize(unicodedata.normalize("NFKC", page_text(page)))
    for endpoint in ("starts_on", "ends_on"):
        entry = raw.get(endpoint)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            invalid(f"{endpoint}: expected a date value and evidence")
            continue
        value, evidence = entry.get("value"), entry.get("evidence")
        quotes = [evidence] if isinstance(evidence, str) else evidence
        if not isinstance(value, str) or not value.strip() or not isinstance(quotes, list) or not quotes:
            invalid(f"{endpoint}: provide a date value and nonempty evidence quotes")
            continue
        if any(not isinstance(q, str) or not q.strip() for q in quotes):
            invalid(f"{endpoint}: evidence quotes must be nonempty strings")
            continue
        if any(_normalize(unicodedata.normalize("NFKC", q)) not in text for q in quotes):
            invalid(f"{endpoint}: each evidence quote must occur in the page")
            continue
        try:
            date = datetime.date.fromisoformat(value)
            if date.isoformat() != value:
                raise ValueError
        except ValueError:
            invalid(f"{endpoint}: value must be a calendar date in YYYY-MM-DD format")
            continue
        supported = [read_range(q, kind="on", said_on=page.published_at) for q in quotes]
        if not any(span and date in (span.start, span.end) for span in supported):
            invalid(f"{endpoint}: evidence must include the stated date, including compressed ranges")
            continue
        dates[endpoint] = date
    start, end = dates.get("starts_on"), dates.get("ends_on")
    if start is not None and end is not None and start > end:
        invalid("time: starts_on must not be after ends_on")
        return {}
    return {} if errors else dates


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
