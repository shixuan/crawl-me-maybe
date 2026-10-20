from __future__ import annotations

from unittest.mock import MagicMock

from crawlme.runtime import EventEmitter, EventType


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
