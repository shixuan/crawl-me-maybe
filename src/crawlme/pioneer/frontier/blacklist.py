"""Domain blacklist.

Loaded from ``blacklist.json`` in the current working directory so users
can edit it without touching ``src/``.  Falls back to a built-in default
if the file is missing or unreadable.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT: frozenset[str] = frozenset({"wikidata.org"})


def _load() -> frozenset[str]:
    path = Path("blacklist.json")
    try:
        data = json.loads(path.read_text())
        domains = data.get("domains", [])
        if isinstance(domains, list) and all(isinstance(d, str) for d in domains):
            return frozenset(domains)
    except Exception:
        logger.debug("blacklist.json not found or invalid, using defaults", exc_info=True)
    return _DEFAULT


DOMAIN_BLACKLIST: frozenset[str] = _load()
