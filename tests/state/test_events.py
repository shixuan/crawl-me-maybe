from __future__ import annotations

from unittest.mock import MagicMock

from crawlme.state.events import EventEmitter, EventType


def test_emit_saves():
    storage = MagicMock()
    emitter = EventEmitter(storage, "t1")
    emitter.emit(EventType.TASK_STARTED, {"max_pages": 10})

    storage.save_event.assert_called_once()
    args = storage.save_event.call_args[0][0]
    assert args["type"] == "TASK_STARTED"
    assert args["task_id"] == "t1"
    assert args["payload"] == {"max_pages": 10}
    assert "ts" in args


def test_emit_default():
    storage = MagicMock()
    emitter = EventEmitter(storage, "t1")
    emitter.emit(EventType.FETCH_COMPLETED)

    args = storage.save_event.call_args[0][0]
    assert args["payload"] == {}


def test_event_types():
    assert EventType.TASK_STARTED == "TASK_STARTED"
    assert EventType.URL_DISCOVERED == "URL_DISCOVERED"
    assert EventType.CANDIDATE_FILTERED == "CANDIDATE_FILTERED"
    assert EventType.FETCH_FAILED == "FETCH_FAILED"
    assert EventType.STOPPED == "STOPPED"


def test_types_are_str():
    """Every EventType attribute should be a plain string."""
    for name in dir(EventType):
        if not name.startswith("_"):
            assert isinstance(getattr(EventType, name), str)
