import gc
from weakref import ref

from crawlme.llm import TokenBudget
from crawlme.pioneer import Frontier
from crawlme.runtime import Limits, Progress, RunState, Stats
from crawlme.scheduler.monitor import StopMonitor
from crawlme.util import subscribe


def _state():
    return RunState(limits=Limits(), progress=Progress(), stats=Stats())


def test_subscriptions_are_per_instance():
    first, second = _state(), _state()
    assert first.progress == second.progress
    first_budget, second_budget = TokenBudget(10), TokenBudget(10)
    first_monitor = StopMonitor(first, Frontier(), first_budget)
    second_monitor = StopMonitor(second, Frontier(), second_budget)
    try:
        first.page_completed()
        assert first_monitor._revision == 1
        assert second_monitor._revision == 0
        second_budget.record(1, 0)
        assert first_monitor._revision == 1
        assert second_monitor._revision == 1
    finally:
        first_monitor.close()
        second_monitor.close()


def test_monitor_close_unsubscribes():
    state, budget = _state(), TokenBudget(10)
    monitor = StopMonitor(state, Frontier(), budget)
    monitor.close()
    monitor.close()
    state.work_started()
    budget.record(1, 0)
    assert monitor._revision == 0


def test_monitor_can_be_collected():
    state, budget = _state(), TokenBudget(10)
    monitor = StopMonitor(state, Frontier(), budget)
    observed = ref(monitor)
    del monitor
    gc.collect()
    assert observed() is None
    state.page_completed()
    budget.record(1, 0)


def test_owner_can_be_collected():
    budget = TokenBudget(10)
    notices = []
    unsubscribe = subscribe(budget, lambda owner: owner.used, lambda: notices.append(True))
    observed = ref(budget)
    del budget
    gc.collect()
    assert observed() is None
    unsubscribe()


def test_unsubscribe_releases_owner():
    def watch_budget():
        budget = TokenBudget(10)
        return subscribe(budget, lambda owner: owner.used, lambda: budget.check()), ref(budget)

    unsubscribe, observed = watch_budget()
    assert observed() is not None
    unsubscribe()
    assert observed() is None
    unsubscribe()
