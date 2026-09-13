"""Shared ID, clock and URL primitives. No imports from pipeline components."""

from __future__ import annotations

import datetime
import hashlib
import uuid

from pydantic import BaseModel


def _new_id() -> str:
    """A fresh event id: every occurrence of the same content gets its
    own id (page rows, analyses under --force, snapshots)."""
    return uuid.uuid4().hex[:12]


def _content_id(text: str) -> str:
    """Return a deterministic content ID using the first 12 SHA-256 hex digits."""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class URL(BaseModel):
    raw: str
    canonical: str
    url_key: str
    scheme: str = ""
    host: str = ""
    path: str = ""
    query: str = ""
    domain: str = ""
    reg_domain: str = ""


class RawLink(BaseModel):
    href: str
    anchor: str | None = None
    snippet: str | None = None
    parent_heading: str | None = None
    position: int = 0
