"""URL canonicalization: normalize equivalent URLs into a single canonical form
and produce a stable url_key fingerprint for deduplication.

Strategy (order matters):
1. Resolve relative URLs against a base
2. Lowercase scheme and host
3. Remove default ports (80 for http, 443 for https)
4. Strip tracking parameters (utm_*, fbclid, gclid, etc.)
5. Collapse duplicate slashes in path
6. Sort query parameters by key (eliminates ?b=1&a=2 vs ?a=2&b=1)

Return (canonical_url, url_key) pairs.

Examples that all produce the same canonical URL + url_key:
    https://EXAMPLE.com:443/a?b=2&a=1&utm_source=x
    https://example.com//a?a=1&b=2
    https://example.com/a?a=1&b=2#fragment
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

# Tracking / marketing query parameters to strip.
# These carry no content and only change for each visitor / campaign.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "ref",
        "source",
        "spm",
        "sc_campaign",
        "mc_cid",
        "mc_eid",
    }
)

_DEFAULT_PORT = {"http": 80, "https": 443}
_DUP_SLASH = re.compile(r"/+")


class Canonicalizer:
    def __init__(self, extra_strip_params: set[str] | None = None) -> None:
        self._strip = set(_TRACKING_PARAMS) | (extra_strip_params or set())

    def canonicalize(self, raw_href: str, base_url: str) -> tuple[str, str]:
        """Return (canonical_url, url_key) for a raw link.

        canonical_url -- the normalized, reproducible form of the URL.
        url_key       -- sha256(canonical_url)[:16], used as the dedup key.
        """
        # 1. Resolve relative links against the page they were found on.
        resolved = urljoin(base_url, raw_href)

        parts = urlparse(resolved)

        # 2. Normalize scheme + host to lowercase.
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()

        # 3. Strip default ports so :443 and no-port are treated as equal.
        port = parts.port
        if port is not None and port == _DEFAULT_PORT.get(scheme):
            netloc = host
        elif port is not None:
            netloc = f"{host}:{port}"
        else:
            netloc = host

        # 4. Collapse runs of slashes (/a//b -> /a/b).
        path = _DUP_SLASH.sub("/", parts.path or "/")

        # 5. Drop tracking params, sort remaining params by key for determinism.
        qs = _clean_query(parts.query, self._strip)

        # 6. Reassemble. Fragment is discarded (not sent to server, so irrelevant).
        canonical = urlunparse((scheme, netloc, path, "", qs, ""))

        # 7. Fingerprint.
        url_key = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        return canonical, url_key


def _clean_query(query: str, strip_params: set[str]) -> str:
    if not query:
        return ""
    cleaned = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k.lower() not in strip_params]
    cleaned.sort(key=lambda x: x[0])
    return urlencode(cleaned) if cleaned else ""
