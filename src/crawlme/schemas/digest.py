"""Digest-layer models: fetch results and extracted pages."""

from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from crawlme.schemas.core import URL, _new_id, _utcnow


class Payload(BaseModel):
    """A selected browser sub-response retained for adapter parsing."""

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
    # Declared publication time; None leaves the source age streak unchanged.
    published_at: datetime.datetime | None = None
    extracted_at: datetime.datetime = Field(default_factory=_utcnow)
    extraction_status: ExtractionStatus = "OK"
