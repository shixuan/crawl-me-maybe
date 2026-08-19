"""Digest-layer models: fetch results and extracted pages."""

from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from crawlme.schemas.core import URL, _new_id, _utcnow


class Payload(BaseModel):
    """One response the page fetched for itself, kept rather than dropped.

    A rendered DOM is what a reader sees, which on a feed listing is
    thumbnails: no post text at all. The text exists — the page asked an
    API for it and used it to build the grid — and keeping that answer
    costs no extra request, only the choice not to throw it away.

    Empty for every fetcher that cannot observe sub-requests, and for
    every run that did not ask to keep any.
    """

    url: str = ""
    content_type: str = ""
    body: bytes = b""


class FetchResult(BaseModel):
    item_id: str
    url_key: str
    url: URL
    status_code: int = 0
    final_url: URL | None = None
    redirects: list[URL] = Field(default_factory=list)
    headers: dict[str, Any] = Field(default_factory=dict[str, Any])
    content_type: str | None = None
    raw: bytes = b""
    payloads: list[Payload] = Field(default_factory=list["Payload"])
    fetch_duration_ms: int = 0
    fetched_at: datetime.datetime = Field(default_factory=_utcnow)
    fetch_attempt: int = 1


ExtractionStatus = Literal["OK", "DEGRADED", "FAILED"]


class Page(BaseModel):
    page_id: str = Field(default_factory=_new_id)
    url_key: str
    url: URL
    raw_html_path: str = ""
    # Where the kept sub-responses landed, in the order they arrived.
    payload_paths: list[str] = Field(default_factory=list)
    title: str | None = None
    markdown: str | None = None
    plain_text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict[str, Any])
    text_hash: str = ""
    text_len: int = 0
    # Publication time as claimed by the page itself (meta tags, JSON-LD,
    # <time>).  Best effort: None means the page did not say, which is
    # different from "published long ago" and is treated as unknown by
    # the TIME_HORIZON stop condition.
    published_at: datetime.datetime | None = None
    extracted_at: datetime.datetime = Field(default_factory=_utcnow)
    extraction_status: ExtractionStatus = "OK"
