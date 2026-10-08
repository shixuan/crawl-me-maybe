"""Derive a goal statement, keywords, publication cutoff and extraction specification.

Return None when unavailable or unsuccessful so the original goal remains usable."""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass
from typing import Any

from crawlme import prompts
from crawlme.config import Settings
from crawlme.llm import LLMClient, LLMError, Stage, TokenBudget, parse_json_response
from crawlme.logging.progress import activity
from crawlme.schemas import CrawlGoal

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _extract_keywords(prompt: str) -> list[str]:
    """Bare tokenization, used when the model call fails."""
    return list(dict.fromkeys(w.lower() for w in _WORD_RE.findall(prompt)))


_MAX_KEYWORDS = 12
_MAX_SPEC_FIELDS = 8
_MAX_SPEC_DESC = 200
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SINCE_MAX_AGE_DAYS = 3650


@dataclass(frozen=True)
class EnhancedGoal:
    """LLM-produced fields to copy onto the CrawlGoal."""

    statement: str
    keywords: list[str]
    since: datetime.datetime | None
    # None means this goal asks to find pages, not to collect fields out
    # of them, and the analyzer keeps its existing shape.
    extraction_spec: dict[str, Any] | None = None
    time_policy: str | None = None


class GoalEnhancer:
    """Enhances a CrawlGoal with one LLM call; None on any failure."""

    def __init__(self, client: LLMClient | None) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> GoalEnhancer:
        """Wire the client with the default-on auto-off semantics: no
        credentials means the enhancer stays inert.  *budget* is shared
        across all LLM consumers of the task."""
        return cls(
            LLMClient.from_settings_if_configured(
                settings,
                budget=budget,
                reasoning_effort=settings.llm_goal_reasoning_effort,
                stage=Stage.GOAL,
            )
        )

    @activity("goal enhancer")
    async def enhance(self, goal: CrawlGoal) -> EnhancedGoal | None:
        """One chat call, then validation.  None means apply nothing."""
        if self._client is None:
            return None
        try:
            resp = await self._client.chat(goal.prompt, system=prompts.goal_system(), json_mode=True)
        except LLMError as e:
            logger.warning("goal.enhance llm error, using raw prompt: %s", e)
            return None
        if not resp.content.strip():
            # Report an empty completion separately from malformed JSON.
            logger.warning("goal.enhance empty content (out=%d), using raw prompt", resp.output_tokens)
            return None
        parsed = self._parse(resp.content)
        if parsed is None:
            logger.warning("goal.enhance unparseable json, using raw prompt")
            return None
        statement, keywords, since, spec, policy = parsed
        if not statement:
            logger.warning("goal.enhance empty statement, using raw prompt")
            return None
        if not keywords:
            keywords = _extract_keywords(goal.prompt)
        return EnhancedGoal(
            statement=statement, keywords=keywords, since=since, extraction_spec=spec, time_policy=policy
        )

    def _parse(
        self, content: str
    ) -> tuple[str, list[str], datetime.datetime | None, dict[str, Any] | None, str | None] | None:
        """Parse the LLM's JSON, tolerating prose wrapped around it."""
        data = parse_json_response(content)
        if data is None:
            return None

        raw_statement = data.get("goal_statement")
        statement = str(raw_statement).strip() if isinstance(raw_statement, str) else ""
        raw_keywords = data.get("keywords")
        if isinstance(raw_keywords, list):
            keywords = [str(k).strip() for k in raw_keywords if isinstance(k, str)]
            keywords = list(dict.fromkeys(keywords))
            keywords = [k for k in keywords if k][:_MAX_KEYWORDS]
        else:
            keywords = []
        since = self._parse_since(data.get("since"))
        spec = self._parse_spec(data.get("extraction_spec"))
        raw_policy = data.get("time_policy")
        policy = raw_policy.strip()[:500] if isinstance(raw_policy, str) else ""
        return statement, keywords, since, spec, policy or None

    def _parse_spec(self, raw: object) -> dict[str, Any] | None:
        """Validate snake_case field names and the optional time-field declaration."""
        if not isinstance(raw, dict):
            return None
        fields = raw.get("fields")
        if not isinstance(fields, dict):
            return None
        clean: dict[str, str] = {}
        for name, desc in fields.items():
            if not isinstance(name, str) or not isinstance(desc, str):
                continue
            key = name.strip().lower()
            if not _FIELD_NAME.match(key) or key in clean:
                continue
            clean[key] = desc.strip()[:_MAX_SPEC_DESC]
            if len(clean) >= _MAX_SPEC_FIELDS:
                break
        if not clean:
            return None
        spec: dict[str, Any] = {"fields": clean}
        # The time field must reference a validated field declaration.
        tf = raw.get("time_field")
        if isinstance(tf, dict):
            name = str(tf.get("name", "")).strip().lower()
            if name in clean:
                spec["time_field"] = {"name": name, "kind": "on" if tf.get("kind") == "on" else "until"}
        return spec

    def _parse_since(self, raw: object) -> datetime.datetime | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            parsed = datetime.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        if parsed > now or parsed < now - datetime.timedelta(days=_SINCE_MAX_AGE_DAYS):
            return None
        return parsed
