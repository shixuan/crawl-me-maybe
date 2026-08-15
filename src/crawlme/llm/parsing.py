"""Tolerant JSON parsing for structured LLM responses."""

from __future__ import annotations

import json
import re
from typing import Any

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def parse_json_response(content: str) -> dict[str, Any] | None:
    """Parse a JSON object out of an LLM response, tolerating slop.

    Models often wrap the JSON in prose, so the first { ... } block is
    extracted and parsed.  When that fails, trailing commas (the most
    common malformation) are stripped and parsing is retried once.
    Shared by every LLM consumer that expects structured output.
    """
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
