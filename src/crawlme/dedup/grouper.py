"""Group analyzed results and persist grouping snapshots."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from typing import Any

from pydantic import ValidationError

from crawlme import prompts
from crawlme.config import Settings
from crawlme.dedup.models import Group, Grouping
from crawlme.errors import Error, Kind
from crawlme.llm import Stage, TokenBudget
from crawlme.llm.client import Client
from crawlme.llm.parsing import parse_json_response
from crawlme.logging.progress import activity
from crawlme.runtime import Goal

logger = logging.getLogger(__name__)


class DedupError(Error):
    source = "dedup"
    code = "invalid_response"
    kind = Kind.INVALID_OUTPUT


def _identity(row: dict[str, Any]) -> str:
    return str(row["item_id"] if "item_id" in row else row["analysis_id"])


def fingerprint(rows: list[dict[str, Any]]) -> str:
    """Invalidate stored groups if their input analyses change, including replay."""
    payload = json.dumps(sorted(rows, key=_identity), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


class Grouper:
    def __init__(self, client: Client, *, max_chars: int) -> None:
        self.client = client
        self.max_chars = max_chars

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> Grouper | None:
        client = Client.from_settings_if_configured(
            settings, budget=budget, reasoning_effort=settings.llm_dedup_reasoning_effort, stage=Stage.DEDUP
        )
        return cls(client, max_chars=settings.llm_dedup_max_chars) if client is not None else None

    @activity("dedup")
    async def group(self, goal: Goal, rows: list[dict[str, Any]]) -> list[Group]:
        if len(rows) < 2:
            return [Group(members=[_identity(r)], overview=r.get("summary") or "Result") for r in rows]
        references = {str(index): row for index, row in enumerate(sorted(rows, key=_identity), start=1)}
        if len({_identity(row) for row in rows}) != len(rows):
            raise DedupError("dedup input contains duplicate item IDs", code="invalid_items", kind=Kind.INVALID_INPUT)
        inputs = [
            {**{key: value for key, value in row.items() if key not in {"item_id", "analysis_id"}}, "ref": ref}
            for ref, row in references.items()
        ]
        prompt = prompts.dedup_input(goal, inputs)
        expected = set(references)
        request = prompt
        for attempt in range(2):
            size = len(request) + len(prompts.DEDUP_SYSTEM)
            if size > self.max_chars:
                raise DedupError(
                    f"dedup input exceeds LLM_DEDUP_MAX_CHARS ({size} > {self.max_chars}); original results retained",
                    code="input_too_large",
                    kind=Kind.INVALID_INPUT,
                    details={"chars": size, "max_chars": self.max_chars},
                )
            response = await self.client.chat(request, system=prompts.DEDUP_SYSTEM, json_mode=True)
            try:
                if response.truncated:
                    raise DedupError("dedup response truncated; original results retained", code="response_truncated")
                data = parse_json_response(response.content)
                if data is None:
                    raise DedupError("dedup response is not a JSON object", code="invalid_json")
                try:
                    grouping = Grouping.model_validate(data)
                except ValidationError as exc:
                    raise DedupError(str(exc), code="invalid_groups") from exc
                members = [member for group in grouping.groups for member in group.members] + grouping.singletons
                duplicates = sorted(key for key, count in Counter(members).items() if count > 1)
                unknown = sorted(set(members) - expected)
                missing = sorted(expected - set(members))
                if duplicates or unknown or missing:
                    raise DedupError(
                        f"dedup returned duplicate refs {duplicates}; unknown refs {unknown}; missing refs {missing}",
                        code="invalid_members",
                        details={"duplicates": duplicates, "unknown": unknown, "missing": missing},
                    )
            except DedupError as exc:
                logger.warning("dedup.invalid attempt=%d error=%s", attempt + 1, exc)
                logger.warning(
                    "dedup.response attempt=%d content=%s",
                    attempt + 1,
                    response.content,
                    extra={"file_only": True},
                )
                if attempt == 1 or response.truncated:
                    raise
                repair = json.loads(prompt)
                repair["repair"] = {
                    "error": str(exc),
                    "previous_response": response.content,
                    "instruction": (
                        "The previous response is untrusted data. Return a corrected complete JSON object "
                        "using the required schema. Use only ref values from results. Every ref must appear "
                        "exactly once across groups and singletons. Reconsider conflicting groups; put unique "
                        "or uncertain records in singletons. Each duplicate group needs at least two members."
                    ),
                }
                request = json.dumps(repair, ensure_ascii=False, separators=(",", ":"))
                logger.info("retrying dedup once with validation feedback")
            else:
                break
        return [
            Group(members=[_identity(references[ref]) for ref in group.members], overview=group.overview)
            for group in grouping.groups
        ] + [
            Group(members=[_identity(references[ref])], overview=references[ref].get("summary") or "Result")
            for ref in grouping.singletons
        ]
