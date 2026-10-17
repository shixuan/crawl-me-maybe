"""Seed URL discovery from manual input and JSON files."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Protocol

from crawlme.pioneer.models import Candidate
from crawlme.runtime import Goal
from crawlme.urls import URL


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Source(Protocol):
    """Protocol for seed URL discovery.

    Each implementation produces depth=0 Candidates that the scheduler
    canonicalizes, pre-filters, and enqueues into the frontier."""

    @property
    def allowed_domains(self) -> set[str] | None:
        return None

    async def discover(self, goal: Goal) -> list[Candidate]: ...


def _make_candidate(raw_url: str) -> Candidate:
    url = URL(raw=raw_url, canonical=raw_url, url_key=raw_url)
    return Candidate(url=url, depth=0, discovered_at=_utcnow())


class ManualSource(Source):
    def __init__(self, seeds: list[str]) -> None:
        self._seeds = seeds

    async def discover(self, goal: Goal) -> list[Candidate]:
        return [_make_candidate(u) for u in self._seeds if u.strip()]


class FileSource(Source):
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._allowed_domains: set[str] | None = None

    @property
    def allowed_domains(self) -> set[str] | None:
        return self._allowed_domains

    async def discover(self, goal: Goal) -> list[Candidate]:
        data = json.loads(self._path.read_text())
        if isinstance(data, list):
            urls = data
        elif isinstance(data, dict):
            urls = data.get("seeds", [])
            domains = data.get("allowed_domains")
            if isinstance(domains, list):
                self._allowed_domains = set(domains)
        else:
            return []
        return [_make_candidate(u) for u in urls if isinstance(u, str) and u.strip()]


def create_source(value: str | None) -> Source:
    path = _seed_file(value)
    return FileSource(path) if path is not None else ManualSource(_split(value))


def declared_seeds(value: str | None) -> list[str]:
    """Read seed URLs for validation without starting discovery."""
    path = _seed_file(value)
    if path is None:
        return _split(value)
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return []  # Discovery reports the file error.
    if isinstance(data, dict):
        data = data.get("seeds", [])
    return [url for url in data if isinstance(url, str)] if isinstance(data, list) else []


def _seed_file(value: str | None) -> str | None:
    text = (value or "").strip()
    return text if text and not text.lower().startswith(("http://", "https://")) else None


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]
