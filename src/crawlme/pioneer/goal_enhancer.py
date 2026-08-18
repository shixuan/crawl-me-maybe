"""Goal Enhancer: one LLM call per task, at task start.

Turns the raw user prompt into three artifacts the pipeline can use:
a full goal statement for the embedding stage (HyDE effect, bilingual
for non-English prompts), a clean keyword list for the rule stage, and
an optional time window for the future time-horizon condition.

Degradation: when the LLM is not configured, fails, or returns
unparseable JSON, enhance() returns None and every ranker keeps its
built-in fallback (bare tokenization, raw prompt), so the crawl never
blocks on the LLM.  The enhancement is additive: the original prompt
stays on the goal and is always embedded alongside the statement.
"""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass
from typing import Any

from crawlme.config import Settings
from crawlme.llm import LLMClient, LLMError, TokenBudget, parse_json_response
from crawlme.pioneer.ranker.rule import _extract_keywords
from crawlme.schemas import CrawlGoal

logger = logging.getLogger(__name__)

_MAX_KEYWORDS = 12
_MAX_SPEC_FIELDS = 8
_MAX_SPEC_DESC = 200
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SINCE_MAX_AGE_DAYS = 3650

# The model cannot know today's date on its own, and time-window goals
# ("recent", "last week") need it to compute since correctly.
_SYSTEM = (
    f"Today is {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d} (UTC). "
    "You turn a user's crawl goal into structured fields. Reply with JSON only, "
    "no prose. Fields: goal_statement (one complete statement of what to find; "
    "if the prompt is not in English, write the statement in English, then append "
    "the same statement in the prompt's language, joined by ' / '), keywords "
    "(array of up to 12 clean content keywords, no stopwords), since (ISO date "
    "YYYY-MM-DD when the goal mentions a time window such as 'recent' or 'last "
    "week', otherwise null), and extraction_spec. "
    "extraction_spec names the fields worth pulling out of every matching page, as "
    '{"fields": {"<snake_case_name>": "<what it holds>"}}. Only produce it when the '
    'goal names things to collect ("with the merchant, the offer and the deadline"); '
    "a goal that just asks to find pages about a subject gets null. At most 8 fields. "
    "Keep every constraint of the original prompt: never narrow the goal."
)


@dataclass(frozen=True)
class EnhancedGoal:
    """LLM-produced fields to copy onto the CrawlGoal."""

    statement: str
    keywords: list[str]
    since: datetime.datetime | None
    # None means this goal asks to find pages, not to collect fields out
    # of them, and the analyzer keeps its existing shape.
    extraction_spec: dict[str, Any] | None = None


class GoalEnhancer:
    """Enhances a CrawlGoal with one LLM call; None on any failure."""

    def __init__(self, client: LLMClient | None) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> GoalEnhancer:
        """Wire the client with the default-on auto-off semantics: no
        credentials means the enhancer stays inert.  *budget* is shared
        across all LLM consumers of the task."""
        return cls(LLMClient.from_settings_if_configured(settings, budget=budget))

    async def enhance(self, goal: CrawlGoal) -> EnhancedGoal | None:
        """One chat call, then validation.  None means apply nothing."""
        if self._client is None:
            return None
        try:
            # Room for the spec on top of the other three fields.  Too
            # small and the JSON truncates, _parse returns None, and the
            # run silently loses keywords and since as well as the spec.
            resp = await self._client.chat(goal.prompt, system=_SYSTEM, max_tokens=1024, json_mode=True)
        except LLMError as e:
            logger.warning("goal.enhance llm error, using raw prompt: %s", e)
            return None
        parsed = self._parse(resp.content)
        if parsed is None:
            logger.warning("goal.enhance unparseable json, using raw prompt")
            return None
        statement, keywords, since, spec = parsed
        if not statement:
            logger.warning("goal.enhance empty statement, using raw prompt")
            return None
        if not keywords:
            keywords = _extract_keywords(goal.prompt)
        return EnhancedGoal(statement=statement, keywords=keywords, since=since, extraction_spec=spec)

    def _parse(self, content: str) -> tuple[str, list[str], datetime.datetime | None, dict[str, Any] | None] | None:
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
        return statement, keywords, since, spec

    def _parse_spec(self, raw: object) -> dict[str, Any] | None:
        """Validate the field list, or return None to extract nothing.

        Field names become keys the whole downstream depends on, so they
        are held to a shape rather than taken as written: anything the
        model invents that is not a plain snake_case name is dropped
        instead of travelling into the analyzer's prompt and out into
        stored results.
        """
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
        return {"fields": clean} if clean else None

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
