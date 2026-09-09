"""Goal Enhancer: one LLM call per task, at task start.

Turns the raw user prompt into three artifacts the pipeline can use:
a full goal statement (HyDE effect, bilingual
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
from crawlme.llm import LLMClient, LLMError, Stage, TokenBudget, parse_json_response
from crawlme.schemas import CrawlGoal

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _parse_constraints(raw: object) -> dict[str, str] | None:
    """Validate the conditions, or return None when the goal states none.

    Held to the same shape as the extraction fields, and for the same
    reason: these names become the ranker's factor names, and a factor
    called whatever the model felt like is one nobody can compare across
    runs.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    clean: dict[str, str] = {}
    for name, desc in raw.items():
        if not isinstance(name, str) or not _FIELD_NAME.fullmatch(name.strip()):
            continue
        text = str(desc).strip() if isinstance(desc, str) else ""
        if text:
            clean[name.strip()] = text
    return dict(list(clean.items())[:_MAX_CONSTRAINTS]) or None


def _extract_keywords(prompt: str) -> list[str]:
    """Bare tokenization, used when the model call fails."""
    return list(dict.fromkeys(w.lower() for w in _WORD_RE.findall(prompt)))


_MAX_KEYWORDS = 12
_MAX_SPEC_FIELDS = 8
_MAX_SPEC_DESC = 200
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# Same cap as the extraction fields: a goal with more conditions than
# this is one nobody could read the scores of either.
_MAX_CONSTRAINTS = 8
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
    '{"fields": {"<snake_case_name>": "<what it holds>"}}. Produce it only when the '
    "goal asks for particular pieces of information out of each page; a goal that asks "
    "to find pages on a subject gets null. Take the fields from the goal's own wording "
    "and stay in its own domain. At most 8 fields. "
    "constraints names the conditions a page must satisfy to count at all, as "
    '{"<snake_case_name>": "<the condition, stated so it can be checked against one page>"}. '
    "These are the goal's filters rather than what to collect: a place, a category, a kind "
    "of subject, a kind of event. List each condition the goal states, one entry each, "
    "neither merged nor split, in English whatever the prompt's language. A goal that "
    "states no conditions gets null. At most 8. "
    "Keep every constraint of the original prompt: never narrow the goal."
)


@dataclass(frozen=True)
class EnhancedGoal:
    """LLM-produced fields to copy onto the CrawlGoal."""

    statement: str
    keywords: list[str]
    since: datetime.datetime | None
    # What a page must satisfy to count, one entry per condition the goal
    # states. The ranker scores a candidate against each of them, so this
    # is the goal's own decomposition rather than one this code invents.
    constraints: dict[str, str] | None = None
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
        return cls(
            LLMClient.from_settings_if_configured(
                settings,
                budget=budget,
                reasoning_effort=settings.llm_enhance_reasoning_effort,
                stage=Stage.GOAL,
            )
        )

    async def enhance(self, goal: CrawlGoal) -> EnhancedGoal | None:
        """One chat call, then validation.  None means apply nothing."""
        if self._client is None:
            return None
        try:
            resp = await self._client.chat(goal.prompt, system=_SYSTEM, json_mode=True)
        except LLMError as e:
            logger.warning("goal.enhance llm error, using raw prompt: %s", e)
            return None
        if not resp.content.strip():
            # Distinct from unparseable: the model wrote nothing at all,
            # which on a reasoning model means it used the whole budget
            # thinking.  Saying so is the difference between a one-look
            # diagnosis and a hunt.
            # On a reasoning model an empty reply means the thinking
            # used the whole ceiling.  Saying so is the difference
            # between a one-look diagnosis and a hunt through the parser.
            logger.warning("goal.enhance empty content (out=%d), using raw prompt", resp.output_tokens)
            return None
        parsed = self._parse(resp.content)
        if parsed is None:
            logger.warning("goal.enhance unparseable json, using raw prompt")
            return None
        statement, keywords, since, spec, constraints = parsed
        if not statement:
            logger.warning("goal.enhance empty statement, using raw prompt")
            return None
        if not keywords:
            keywords = _extract_keywords(goal.prompt)
        return EnhancedGoal(
            statement=statement, keywords=keywords, since=since, extraction_spec=spec, constraints=constraints
        )

    def _parse(
        self, content: str
    ) -> tuple[str, list[str], datetime.datetime | None, dict[str, Any] | None, dict[str, str] | None] | None:
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
        return statement, keywords, since, spec, _parse_constraints(data.get("constraints"))

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
