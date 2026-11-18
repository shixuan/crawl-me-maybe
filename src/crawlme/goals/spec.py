"""Read and version goal extraction specifications."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def spec_fields(spec: dict[str, Any] | None) -> dict[str, str]:
    """Return declared field names and descriptions, or an empty mapping."""
    if not isinstance(spec, dict):
        return {}
    fields = spec.get("fields")
    if not isinstance(fields, dict):
        return {}
    return {str(k): str(v) for k, v in fields.items() if isinstance(k, str)}


def spec_time_field(spec: dict[str, Any] | None) -> tuple[str, str] | None:
    """Return the declared event-time field and on/until meaning, or None."""
    if not isinstance(spec, dict):
        return None
    raw = spec.get("time_field")
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    kind = raw.get("kind")
    if not isinstance(name, str) or name not in spec_fields(spec):
        return None
    return name, ("on" if kind == "on" else "until")


def spec_version(spec: dict[str, Any] | None, time_policy: str | None = None) -> str:
    """Hash the extraction specification independently of the prompt-derived goal ID."""
    fields = spec_fields(spec)
    if not fields and not time_policy:
        return ""
    content = {"fields": fields, "time_policy": time_policy} if time_policy else fields
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
