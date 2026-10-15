"""Common error facts, with codes and original causes owned by components."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType


class Kind(str, Enum):
    LIMIT_REACHED = "limit_reached"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INVALID_INPUT = "invalid_input"
    INVALID_OUTPUT = "invalid_output"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class Scope(str, Enum):
    OPERATION = "operation"
    DEPENDENCY = "dependency"


class Error(Exception):
    source: str = ""
    code: str = ""
    kind: Kind = Kind.UNKNOWN
    scope: Scope = Scope.OPERATION

    def __init__(
        self,
        message: str,
        *,
        source: str | None = None,
        code: str | None = None,
        kind: Kind | None = None,
        scope: Scope | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.source = type(self).source if source is None else source
        self.code = type(self).code if code is None else code
        self.kind = type(self).kind if kind is None else kind
        self.scope = type(self).scope if scope is None else scope
        if not self.source or not self.code:
            raise ValueError("errors require a source and code")
        super().__init__(message)
        self.details: Mapping[str, object] = MappingProxyType(dict(details or {}))
