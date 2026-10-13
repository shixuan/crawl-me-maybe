"""URL identity and links discovered in page content."""

from pydantic import BaseModel


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
