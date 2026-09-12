"""Turning internals into the words a log line uses."""

from __future__ import annotations

# Past this a URL wraps the terminal and stops being scannable.
_MAX = 70


def where(url: str) -> str:
    """Format an address for human-readable progress logs."""
    short = url.split("://", 1)[-1].removeprefix("www.").rstrip("/")
    return short if len(short) <= _MAX else short[: _MAX - 1] + "\u2026"
