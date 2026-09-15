from crawlme.runtime.state import Limits, Progress, RunState, Stats
from crawlme.scheduler.reporting import summary


def test_report_does_not_create_sources():
    state = RunState(limits=Limits(), progress=Progress(), stats=Stats())
    state.proposed_seeds["missing"] = ("https://example.com/", "why")
    state.rejected_seeds.append(("https://missing.example/", "unavailable"))

    report = summary(state)

    assert report["proposed_seeds"] == {"https://example.com/": ("why", (0, 0, 0, 0, 0, 0))}
    assert not state.seeds
    report["rejected_seeds"].clear()
    assert state.rejected_seeds == [("https://missing.example/", "unavailable")]
