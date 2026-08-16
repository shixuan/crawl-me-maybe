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
from dataclasses import dataclass

from crawlme.config import Settings
from crawlme.llm import LLMClient, LLMError, TokenBudget, parse_json_response
from crawlme.pioneer.ranker.rule import _extract_keywords
from crawlme.schemas import CrawlGoal

logger = logging.getLogger(__name__)

_MAX_KEYWORDS = 12
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
    "week', otherwise null). Keep every constraint of the original prompt: never "
    "narrow the goal."
)


@dataclass(frozen=True)
class EnhancedGoal:
    """LLM-produced fields to copy onto the CrawlGoal."""

    statement: str
    keywords: list[str]
    since: datetime.datetime | None


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
            resp = await self._client.chat(goal.prompt, system=_SYSTEM, max_tokens=512, json_mode=True)
        except LLMError as e:
            logger.warning("goal.enhance llm error, using raw prompt: %s", e)
            return None
        parsed = self._parse(resp.content)
        if parsed is None:
            logger.warning("goal.enhance unparseable json, using raw prompt")
            return None
        statement, keywords, since = parsed
        if not statement:
            logger.warning("goal.enhance empty statement, using raw prompt")
            return None
        if not keywords:
            keywords = _extract_keywords(goal.prompt)
        return EnhancedGoal(statement=statement, keywords=keywords, since=since)

    def _parse(self, content: str) -> tuple[str, list[str], datetime.datetime | None] | None:
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
        return statement, keywords, since

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
