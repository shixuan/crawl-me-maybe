"""Shared primitives: id/clock helpers and the URL vocabulary.

The schemas package is the neutral cross-layer vocabulary: every layer
imports from it, it imports from none of them (discipline rule: keep
it dependency-free and behavior-free, so it can never create a cycle).
"""

from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


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
