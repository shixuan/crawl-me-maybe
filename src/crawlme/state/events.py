"""Event emitter: append-only audit trail for the crawl state machine.

Every significant state transition gets recorded in the events table
(append-only, seq-ordered).  Events are the foundation for:
  - real-time progress display (CLI tails by seq)
  - post-crawl audit (why was this URL dropped?)
  - replay / debugging

Event types cover the full state machine referenced in arch.md:
  TASK_STARTED -> URL_DISCOVERED -> CANDIDATE_BUFFERED -> ... -> STOPPED
"""

from __future__ import annotations

import datetime
from typing import Any


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# event type constants ------------------------------------------------


class EventType:
    TASK_STARTED = "TASK_STARTED"
    TASK_PAUSED = "TASK_PAUSED"
    TASK_RESUMED = "TASK_RESUMED"
    TASK_STOPPED = "TASK_STOPPED"

    URL_DISCOVERED = "URL_DISCOVERED"
    CANDIDATE_FILTERED = "CANDIDATE_FILTERED"
    CANDIDATE_BUFFERED = "CANDIDATE_BUFFERED"
    CANDIDATE_DROPPED = "CANDIDATE_DROPPED"
    CANDIDATE_ENQUEUED = "CANDIDATE_ENQUEUED"

    FETCH_STARTED = "FETCH_STARTED"
    FETCH_COMPLETED = "FETCH_COMPLETED"
    FETCH_FAILED = "FETCH_FAILED"

    PAGE_EXTRACTED = "PAGE_EXTRACTED"
    CHECKPOINT_SAVED = "CHECKPOINT_SAVED"
    STOPPED = "STOPPED"


# emitter ------------------------------------------------------------


class EventEmitter:
    """Thin wrapper around Storage.save_event that auto-fills ts.

    Typical usage in the scheduler:
        events = EventEmitter(storage, task_id)
        events.emit(EventType.FETCH_STARTED, {"url_key": item.url_key})
    """

    def __init__(self, storage: Any, task_id: str) -> None:
        self._storage = storage
        self._task_id = task_id

    def emit(self, type_: str, payload: dict[str, Any] | None = None) -> None:
        self._storage.save_event(
            {
                "ts": _utcnow().isoformat(),
                "task_id": self._task_id,
                "type": type_,
                "payload": payload or {},
            }
        )
