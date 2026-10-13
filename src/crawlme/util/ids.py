"""Occurrence and content identifiers."""

import hashlib
import uuid


def _new_id() -> str:
    """A fresh event id: every occurrence of the same content gets its
    own id (page rows, analyses under --force, snapshots)."""
    return uuid.uuid4().hex[:12]


def _content_id(text: str) -> str:
    """Return a deterministic content ID using the first 12 SHA-256 hex digits."""
    return hashlib.sha256(text.encode()).hexdigest()[:12]
