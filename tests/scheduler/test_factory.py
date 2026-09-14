from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from crawlme.config import Settings
from crawlme.pioneer.ranker import LLMRanker
from crawlme.scheduler.factory import _build_ranker, create_scheduler


def test_ranker_is_llm(tmp_path: Path):
    """One stage is left, and the builder hands it straight back."""
    llm = MagicMock(spec=LLMRanker)
    assert _build_ranker(Settings(result_dir=tmp_path), llm=llm) is llm


def test_ranker_no_creds(tmp_path: Path):
    """No LLM, no ranker.  The engine fetches in frontier order rather
    than in an order the rule stage was measured not to improve."""
    assert _build_ranker(Settings(result_dir=tmp_path)) is None


def test_sched_no_ranker(tmp_path: Path):
    """A scheduler with no credentials still builds and still crawls."""
    sched = create_scheduler(Settings(result_dir=tmp_path, llm_api_key="", llm_base_url=""))
    assert sched._ranking.ranker is None


def test_sched_analyzer(tmp_path: Path):
    """A passed analyzer reaches the engine, which binds its sink."""
    analyzer = MagicMock()
    sched = create_scheduler(Settings(result_dir=tmp_path), analyzer=analyzer)
    assert sched._analysis.analyzer is analyzer
    analyzer.bind_sink.assert_called_once()


def test_scheduler_no_analyzer(tmp_path: Path):
    """analysis_enabled off: the engine runs with the subsystem absent."""
    cfg = Settings(result_dir=tmp_path, analysis_enabled=False)
    sched = create_scheduler(cfg)
    assert sched._analysis.analyzer is None


def test_sched_run_state(tmp_path: Path):
    """The engine and tracker use the state supplied to the factory."""
    from crawlme.runtime.state import Limits, Progress, RunState, Stats

    state = RunState(limits=Limits(), progress=Progress(), stats=Stats())
    sched = create_scheduler(Settings(result_dir=tmp_path), run_state=state)
    assert sched.run_state is state
    assert sched._tracking.state is state
