from crawlme.runtime.clock import RunClock


def test_startup_and_crawl_share_budget_and_resume_excludes_downtime(monkeypatch):
    now = 100.0
    monkeypatch.setattr("crawlme.runtime.clock.monotonic", lambda: now)
    clock = RunClock()
    clock.start(120)

    now += 40
    clock.start(120)
    assert clock.remaining() == 80
    saved = clock.elapsed()

    now += 300
    resumed = RunClock()
    resumed.start(120)
    resumed.restore(saved)
    assert resumed.remaining() == 80
    now += 80
    assert resumed.expired()
    assert resumed.elapsed() == 120
