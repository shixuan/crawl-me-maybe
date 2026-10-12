"""Run persistence, read-only results and process locking."""

from crawlme.persist.lock import Lock
from crawlme.persist.read import Results, read_results
from crawlme.persist.storage import Storage

__all__ = ["Lock", "Results", "Storage", "read_results"]
