"""Shared primitives: id/clock helpers and the URL vocabulary.

The schemas package is the neutral cross-layer vocabulary: every layer
imports from it, it imports from none of them (discipline rule: keep
it dependency-free and behavior-free, so it can never create a cycle).
"""

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
    """A content-derived id: same text, same id (sha256[:12]).

    For entities whose identity IS their content — a goal is named by
    its prompt, so two runs (or replays) with the same prompt share
    the goal id, which is what makes same-prompt replay idempotent and
    replay idempotent.
    """
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
