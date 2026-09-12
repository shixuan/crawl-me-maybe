"""Normalize URLs and derive their canonical identity.

Domain grouping is a heuristic, not a public-suffix lookup."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from crawlme.schemas import URL

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

# Common subdomains that don't change the registrable domain.
_SUBDOMAIN_PREFIXES = frozenset(
    {
        "www",
        "m",
        "api",
        "cdn",
        "static",
        "assets",
        "media",
        "files",
        "docs",
        "blog",
        "shop",
        "store",
        "mail",
        "news",
        "dev",
        "staging",
        "test",
        "app",
    }
)


class Canonicalizer:
    def __init__(self, extra_strip_params: set[str] | None = None) -> None:
        self._strip = set(_TRACKING_PARAMS) | (extra_strip_params or set())

    def canonicalize(self, raw_href: str, base_url: str) -> URL:
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

        # 5. Drop tracking params, sort remaining params by key.
        query = _clean_query(parts.query, self._strip)

        # 6. Reassemble.  Fragment is discarded.
        canonical = urlunparse((scheme, netloc, path, "", query, ""))

        # 7. Fingerprint.
        url_key = hashlib.sha256(canonical.encode()).hexdigest()[:16]

        domain = host
        reg_domain = _extract_reg_domain(host)

        return URL(
            raw=raw_href,
            canonical=canonical,
            url_key=url_key,
            scheme=scheme,
            host=host,
            path=path,
            query=query,
            domain=domain,
            reg_domain=reg_domain,
        )


def _clean_query(query: str, strip_params: set[str]) -> str:
    if not query:
        return ""
    cleaned = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k.lower() not in strip_params]
    cleaned.sort(key=lambda x: x[0])
    return urlencode(cleaned) if cleaned else ""


def _extract_reg_domain(host: str) -> str:
    """Approximate a registrable domain; multi-part suffixes such as co.uk are not handled."""
    if not host:
        return ""
    labels = host.split(".")
    # Strip known subdomains from the front.
    while len(labels) > 2 and labels[0].lower() in _SUBDOMAIN_PREFIXES:
        labels.pop(0)
    return ".".join(labels[-3:] if len(labels) >= 3 else labels)
