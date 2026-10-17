from __future__ import annotations

from crawlme.runtime import Task


def test_task_defaults():
    ct = Task()
    assert ct.task_id
    assert ct.state == "CREATED"
