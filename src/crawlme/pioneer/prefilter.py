"""Pre-filter: zero-LLM deterministic rules that discard junk candidates.

Each rule is an independent callable returning (ALLOW | DROP, reason_str).
Rules execute in priority order and short-circuit on first DROP.
Fail-open on rule exceptions: a broken rule never blocks a candidate.
"""

from __future__ import annotations

import datetime
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from crawlme.schemas import Candidate, CrawlGoal

logger = logging.getLogger(__name__)


class Decision(Enum):
    ALLOW = "allow"
    DROP = "drop"


@dataclass
class PreFilterContext:
    visited: set[str] = field(default_factory=set)
    frontier_keys: set[str] = field(default_factory=set)
    domain_counters: dict[str, int] = field(default_factory=dict)
    allow_fetch: Callable[[str], bool] | None = None
    allowed_domains: set[str] | None = None

    def is_visited_or_queued(self, url_key: str) -> bool:
        return url_key in self.visited or url_key in self.frontier_keys


RuleFunc = Callable[[Candidate, CrawlGoal, PreFilterContext], tuple[Decision, str] | None]
"""A rule returns (Decision, rule_name) or None to skip."""


_EXT_DENYLIST = re.compile(
    r"\.(jpg|jpeg|png|gif|svg|ico|webp|mp4|mp3|avi|mov|wmv|flv"
    r"|pdf|doc|docx|xls|xlsx|ppt|pptx"
    r"|zip|rar|tar|gz|7z"
    r"|exe|dmg|deb|rpm|msi"
    r"|css|js|map|txt|xml|rss)"
    r"(\?|#|$)",
    re.IGNORECASE,
)

_URL_PATTERN_DENYLIST = re.compile(
    r"/(login|logout|signup|register|signin|cart|checkout"
    r"|account|admin|wp-admin|ajax|api/)/"
    r"|[\U00002600-\U000027BF\U0001F300-\U0001F9FF\U0000FE00-\U0000FE0F]",  # emoji
    re.IGNORECASE,
)

_NEGATIVE_ANCHOR = re.compile(
    r"^(download|click\s*here|read\s*more|more|here)$",
    re.IGNORECASE,
)


#: rules ----------------------------------------------------------------


def blacklist_check(c: Candidate, goal: CrawlGoal, ctx: PreFilterContext) -> tuple[Decision, str] | None:
    from crawlme.pioneer.blacklist import DOMAIN_BLACKLIST

    if c.url.reg_domain in DOMAIN_BLACKLIST or c.url.domain in DOMAIN_BLACKLIST:
        return Decision.DROP, "blacklist"
    return None


def scope_check(c: Candidate, goal: CrawlGoal, ctx: PreFilterContext) -> tuple[Decision, str] | None:
    if ctx.allowed_domains and c.url.reg_domain not in ctx.allowed_domains:
        return Decision.DROP, "scope"
    return None


def dedup_check(c: Candidate, goal: CrawlGoal, ctx: PreFilterContext) -> tuple[Decision, str] | None:
    if ctx.is_visited_or_queued(c.url.url_key):
        return Decision.DROP, "dedup"
    return None


def robots_check(c: Candidate, goal: CrawlGoal, ctx: PreFilterContext) -> tuple[Decision, str] | None:
    if ctx.allow_fetch is None:
        return None
    if not ctx.allow_fetch(c.url.canonical):
        return Decision.DROP, "robots"
    return None


def protocol_check(c: Candidate, goal: CrawlGoal, ctx: PreFilterContext) -> tuple[Decision, str] | None:
    if c.url.raw.startswith(("javascript:", "mailto:", "tel:", "#")):
        return Decision.DROP, "protocol"
    return None


def extension_check(c: Candidate, goal: CrawlGoal, ctx: PreFilterContext) -> tuple[Decision, str] | None:
    if _EXT_DENYLIST.search(c.url.canonical):
        return Decision.DROP, "extension"
    return None


def url_pattern_check(c: Candidate, goal: CrawlGoal, ctx: PreFilterContext) -> tuple[Decision, str] | None:
    if _URL_PATTERN_DENYLIST.search(c.url.path):
        return Decision.DROP, "url_pattern"
    return None


def depth_check(c: Candidate, goal: CrawlGoal, ctx: PreFilterContext) -> tuple[Decision, str] | None:
    if c.depth > goal.depth_limit:
        return Decision.DROP, "depth"
    return None


def domain_budget_check(c: Candidate, goal: CrawlGoal, ctx: PreFilterContext) -> tuple[Decision, str] | None:
    used = ctx.domain_counters.get(c.url.reg_domain, 0)
    if goal.domain_budget > 0 and used >= goal.domain_budget:
        return Decision.DROP, "domain_budget"
    return None


def negative_anchor_check(c: Candidate, goal: CrawlGoal, ctx: PreFilterContext) -> tuple[Decision, str] | None:
    if c.anchor and _NEGATIVE_ANCHOR.match(c.anchor):
        return Decision.DROP, "negative_anchor"
    return None


def stale_check(c: Candidate, goal: CrawlGoal, _ctx: PreFilterContext) -> tuple[Decision, str] | None:
    """Drop candidates a listing already dated outside the goal's window.

    A listing states roughly when each item was posted, so a post older
    than the window can be skipped before paying a request to read it.
    That saving is the whole reason a feed still wants a funnel.

    Only a *stated* date drops anything. An unknown date is not an old
    one, and platforms leave it out often enough to matter: four of the
    twelve entries on the page this was written against carried no date
    at all. Guessing there would silently discard fresh posts.

    This is the per-candidate half of the time window. TIME_HORIZON is
    the other half and stops a whole run, which is right only for a
    strictly ordered source; a monitoring run over many accounts must not
    stop because one quiet account's posts came up first.
    """
    if goal.since is None or c.posted_at is None:
        return None
    posted = c.posted_at
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=datetime.timezone.utc)
    if posted < goal.since:
        return Decision.DROP, "stale"
    return None


# -----------------------------------------------------------------------


class PreFilter:
    def __init__(self, enable_negative_anchor: bool = False) -> None:
        rules: list[RuleFunc] = [
            scope_check,
            dedup_check,
            blacklist_check,
            robots_check,
            protocol_check,
            extension_check,
            url_pattern_check,
            depth_check,
            domain_budget_check,
            stale_check,
        ]
        if enable_negative_anchor:
            rules.append(negative_anchor_check)
        self._rules = rules

    def check(self, c: Candidate, goal: CrawlGoal, ctx: PreFilterContext) -> tuple[Decision, str]:
        for rule in self._rules:
            try:
                result = rule(c, goal, ctx)
                if result is not None:
                    return result
            except Exception:
                logger.warning("prefilter.rule_error rule=%s url=%s", rule.__name__, c.url.raw, exc_info=True)
                continue
        return Decision.ALLOW, ""
