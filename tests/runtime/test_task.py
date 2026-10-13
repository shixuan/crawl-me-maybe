from __future__ import annotations

from crawlme.runtime import CrawlTask


def test_task_defaults():
    ct = CrawlTask()
    assert ct.task_id
    assert ct.state == "CREATED"
