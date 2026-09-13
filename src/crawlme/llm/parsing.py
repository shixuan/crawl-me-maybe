"""Tolerant JSON parsing for structured LLM responses."""

from __future__ import annotations

import json
import re
from typing import Any

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def parse_json_response(content: str) -> dict[str, Any] | None:
    """Parse a JSON object, tolerating surrounding prose and retrying without trailing commas."""
    match = _JSON_BLOCK_RE.search(content)
    if match is None:
        return None
    block = match.group()
    for attempt in (block, _TRAILING_COMMA_RE.sub(r"\1", block)):
        try:
            data = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None
