"""Read, validate and merge browser session files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from crawlme.errors import Error, Kind, Scope


def load_sessions(paths: list[str]) -> dict[str, Any] | None:
    """Merge session files in order without modifying their contents."""
    if not paths:
        return None
    cookies: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    origins: dict[str, dict[str, Any]] = {}
    for path in paths:
        state = _read_storage_state(path)
        for cookie in state.get("cookies", []):
            key = (cookie["name"], cookie.get("domain", ""), cookie.get("path", "/"), cookie.get("partitionKey", ""))
            cookies[key] = cookie
        for origin in state.get("origins", []):
            previous = origins.get(origin["origin"], {})
            storage = {item["name"]: item for item in previous.get("localStorage", [])}
            storage.update({item["name"]: item for item in origin.get("localStorage", [])})
            origins[origin["origin"]] = {**previous, **origin, "localStorage": list(storage.values())}
    return {"cookies": list(cookies.values()), "origins": list(origins.values())}


def _read_storage_state(path: str) -> dict[str, Any]:
    """Load saved session state; reject invalid input instead of silently browsing anonymously."""
    p = Path(path)
    if not p.is_file():
        raise Error(
            f"storage state file not found: {path}",
            source="session",
            code="session_missing",
            scope=Scope.DEPENDENCY,
            kind=Kind.NOT_FOUND,
            details={"path": path},
        )
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise Error(
            f"storage state file is not readable JSON: {path}",
            source="session",
            code="session_invalid",
            scope=Scope.DEPENDENCY,
            kind=Kind.INVALID_INPUT,
            details={"path": path},
        ) from e
    if not isinstance(state, dict) or not (state.get("cookies") or state.get("origins")):
        raise Error(
            f"storage state file has no cookies or origins: {path}",
            source="session",
            code="session_invalid",
            scope=Scope.DEPENDENCY,
            kind=Kind.INVALID_INPUT,
            details={"path": path},
        )
    try:
        for field in ("cookies", "origins"):
            if not isinstance(state.get(field, []), list):
                raise ValueError
        for cookie in state.get("cookies", []):
            if (
                not isinstance(cookie, dict)
                or not isinstance(cookie.get("name"), str)
                or not isinstance(cookie.get("value"), str)
            ):
                raise ValueError
            if any(not isinstance(cookie.get(key, ""), str) for key in ("domain", "path", "partitionKey")):
                raise ValueError
        for origin in state.get("origins", []):
            if not isinstance(origin, dict) or not isinstance(origin.get("origin"), str):
                raise ValueError
            storage = origin.get("localStorage", [])
            if not isinstance(storage, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("value"), str)
                for item in storage
            ):
                raise ValueError
    except ValueError as exc:
        raise Error(
            f"invalid cookies or origins in storage state file: {path}",
            source="session",
            code="session_invalid",
            scope=Scope.DEPENDENCY,
            kind=Kind.INVALID_INPUT,
            details={"path": path},
        ) from exc
    return state
