"""Classify pages and extract fields with source evidence.

Return supported results or raise an error for the caller to handle."""

from __future__ import annotations

import json
import logging
from typing import Any

from crawlme import prompts
from crawlme.analysis.models import AnalysisResult
from crawlme.analysis.validator import AnalysisError, item_entries, page_text, validate_analysis, validate_time
from crawlme.config import Settings
from crawlme.digest import Page
from crawlme.goals import Goal
from crawlme.llm import Client, Stage, TokenBudget, parse_json_response
from crawlme.logging import where
from crawlme.logging.progress import activity

logger = logging.getLogger(__name__)


# Default analyzer text limit; Settings can override it.
_MAX_PAGE_CHARS = 3000


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
        if page.sources:
            remaining = self._max_page_chars
            sources = []
            for source in page.sources:
                if remaining <= 0:
                    break
                sources.append(source.model_copy(update={"text": source.text[:remaining]}))
                remaining -= len(source.text)
            page = page.model_copy(update={"sources": sources, "plain_text": "\n\n".join(s.text for s in sources)})
        text = page_text(page)
        if not text:
            return None
        prompt = prompts.analysis_input(goal, page, text, self._max_page_chars)
        resp = await self._client.chat(prompt, system=prompts.analysis_system(goal), json_mode=True)
        try:
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
            entries = item_entries(data)
            if goal.time_policy and data.get("classification") == "RELEVANT":
                for entry in entries:
                    corrected, used = await self._correct_time(entry, page, goal, prompt)
                    entry.update(corrected)
                    tokens += used
            result = validate_analysis(data, page, goal, model=resp.model, tokens_used=tokens)
        except AnalysisError as exc:
            logger.warning("analysis.invalid url_key=%s code=%s error=%s", page.url_key, exc.code, exc)
            logger.debug(
                "analysis.response url_key=%s content=%s", page.url_key, resp.content, extra={"file_only": True}
            )
            raise
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
            validate_time(data, page, issues=issues)
            if issues:
                logger.info("analysis.time_retry url_key=%s errors=%s", page.url_key, "; ".join(issues))
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
                        + "Always include time.status; explain not_stated or ambiguous with time.reason.",
                        json_mode=True,
                    )
                    tokens += fixed.input_tokens + fixed.output_tokens
                    if fixed.truncated:
                        raise AnalysisError("time correction truncated", code="invalid_dates")
                    corrected = parse_json_response(fixed.content)
                    if corrected is None or not isinstance(corrected.get("time"), dict):
                        raise AnalysisError("time correction must return a time object", code="invalid_dates")
                    remaining: list[str] = []
                    validate_time(corrected, page, issues=remaining)
                    if remaining:
                        raise AnalysisError(
                            "; ".join(remaining), code="invalid_dates", details={"url_key": page.url_key}
                        )
                    data = {**data, "time": corrected["time"]}
                except AnalysisError as exc:
                    logger.debug("analysis.time_failed url_key=%s error=%s", page.url_key, exc)
                    raise
        return data, tokens
