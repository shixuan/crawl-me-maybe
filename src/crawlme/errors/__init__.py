"""Shared error contracts and recovery policies."""

from crawlme.errors.errors import Error, Kind, Scope
from crawlme.errors.handler import ErrorHandler, Skipped
from crawlme.errors.policy import Action, Decision, ErrorContext, ErrorPolicy

__all__ = ["Action", "Decision", "Error", "ErrorContext", "ErrorHandler", "ErrorPolicy", "Kind", "Scope", "Skipped"]
