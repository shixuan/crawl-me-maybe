"""Validate grouping boundaries, persistence and scheduler failure isolation."""

import json
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.config import Settings
from crawlme.dedup.grouper import Grouper, fingerprint
from crawlme.llm import LLMResponse, TokenBudget
from crawlme.scheduler.factory import create_scheduler
from crawlme.schemas import URL, AnalysisResult, CrawlGoal, Page
from crawlme.storage.sqlite import SqliteStorage


def client_response(groups, *, truncated=False):
    client = MagicMock()
    client.chat = AsyncMock(return_value=LLMResponse(json.dumps({"groups": groups}), 100, 20, "test", truncated))
    return client


@pytest.mark.parametrize(
    "groups",
    [
        [{"members": ["a", "a", "b"], "overview": "duplicate"}],
        [{"members": ["a", "b", "invented"], "overview": "unknown"}],
        [{"members": [], "overview": "empty"}],
    ],
)
async def test_invalid_partition_rejected(groups):
    grouper = Grouper(client_response(groups), max_chars=10000)
    with pytest.raises(ValueError if not groups[0]["members"] else Exception):
        await grouper.group(CrawlGoal(prompt="gifts"), [{"analysis_id": "a"}, {"analysis_id": "b"}])


async def test_group_preserves_evidence_and_does_not_output_a_score():
    client = client_response([{"members": ["a", "b"], "overview": "Same offer; terms differ."}])
    rows = [
        {"analysis_id": "a", "extracted": {"condition": {"value": "two large", "evidence": "two large"}}},
        {"analysis_id": "b", "extracted": {"condition": {"value": "any two", "evidence": "any two"}}},
    ]
    result = await Grouper(client, max_chars=10000).group(CrawlGoal(prompt="gifts"), rows)
    assert result[0].members == ["a", "b"]
    assert json.loads(client.chat.call_args.args[0])["results"] == rows
    assert set(result[0].model_dump()) == {"members", "overview"}


async def test_no_truncation_or_call_for_oversized_input():
    client = client_response([])
    with pytest.raises(Exception, match="exceeds"):
        await Grouper(client, max_chars=10).group(
            CrawlGoal(prompt="gifts"), [{"analysis_id": "a"}, {"analysis_id": "b"}]
        )
    client.chat.assert_not_called()


async def test_truncated_output_rejected():
    client = client_response([{"members": ["a", "b"], "overview": "offer"}], truncated=True)
    with pytest.raises(Exception, match="truncated"):
        await Grouper(client, max_chars=10000).group(
            CrawlGoal(prompt="gifts"), [{"analysis_id": "a"}, {"analysis_id": "b"}]
        )


async def test_singleton_needs_no_llm():
    client = client_response([])
    groups = await Grouper(client, max_chars=10000).group(
        CrawlGoal(prompt="gifts"), [{"analysis_id": "a", "summary": "offer"}]
    )
    assert groups[0].members == ["a"]
    client.chat.assert_not_called()


async def test_omitted_ids_are_preserved_as_singletons():
    rows = [{"analysis_id": "a", "summary": "one"}, {"analysis_id": "b", "summary": "two"}]
    groups = await Grouper(client_response([]), max_chars=10000).group(CrawlGoal(prompt="gifts"), rows)
    assert [g.members for g in groups] == [["a"], ["b"]]


async def test_persisted_groups_dashboard_and_stale_replay(tmp_path):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
    import serve

    storage = SqliteStorage.create(tmp_path)
    await storage.start()
    goal = CrawlGoal(prompt="gifts")
    storage.save_goal(goal.model_dump(mode="json"))
    for n, score in enumerate([0.9, 0.7]):
        page = Page(
            url=URL(raw=f"https://example.com/{n}", canonical=f"https://example.com/{n}", url_key=str(n)),
            url_key=str(n),
        )
        storage.save_page(page)
        result = AnalysisResult(
            page_id=page.page_id,
            url_key=str(n),
            goal_id=goal.goal_id,
            classification="RELEVANT",
            relevance_score=score,
            summary="offer",
        )
        storage.save_analysis(result.model_dump(mode="json"))
    try:
        rows = await storage.dedup_inputs(goal.goal_id)
        ids = [r["analysis_id"] for r in rows]
        await storage.save_groups(
            goal.goal_id, fingerprint(rows), [{"members": ids, "overview": "one offer"}], model="test"
        )
        with sqlite3.connect(storage.db_path) as con:
            con.row_factory = sqlite3.Row
            assert serve._groups(con, goal.goal_id)[0]["members"] == ids
            # Failed publication must roll back the snapshot as well as its groups.
            with pytest.raises(sqlite3.IntegrityError):
                await storage.save_groups(
                    goal.goal_id,
                    fingerprint(rows),
                    [{"members": ids + ids, "overview": "bad"}],
                    model="test",
                )
            assert con.execute("SELECT count(*) FROM dedup_runs").fetchone()[0] == 1
            assert con.execute("SELECT count(*) FROM analyses").fetchone()[0] == 2
            con.execute("UPDATE analyses SET summary = 'changed after replay'")
            con.commit()
            assert serve._groups(con, goal.goal_id) == []
    finally:
        await storage.close()


async def test_group_inputs_match_readers(tmp_path, monkeypatch):
    """Both consumers use the same goal, page revision and evidence for fingerprints."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
    import serve

    storage = SqliteStorage.create(tmp_path)
    await storage.start()
    try:
        for key in ("b", "a"):
            page = Page(
                page_id=f"page-{key}",
                url_key="shared-url",
                url=URL(raw="https://example.com/offer", canonical="https://example.com/offer", url_key="shared-url"),
            )
            storage.save_page(page)
            storage.save_analysis(
                {
                    "analysis_id": key,
                    "page_id": page.page_id,
                    "url_key": page.url_key,
                    "goal_id": "chosen",
                    "classification": "RELEVANT",
                    "relevance_score": 0.9,
                    "summary": key,
                    "extracted": {"offer": {"value": key, "evidence": f"offer {key}"}},
                }
            )
        for key, goal, classification, page_id in (
            ("other-goal", "other", "RELEVANT", "page-a"),
            ("discarded", "chosen", "IRRELEVANT", "page-a"),
            ("orphan", "chosen", "RELEVANT", "missing-page"),
        ):
            storage.save_analysis(
                {"analysis_id": key, "goal_id": goal, "classification": classification, "page_id": page_id}
            )
        rows = await storage.dedup_inputs("chosen")
        assert [row["analysis_id"] for row in rows] == ["a", "b"]
        assert [row["extracted"]["offer"]["value"] for row in rows] == ["a", "b"]
        await storage.save_groups(
            "chosen", fingerprint(rows), [{"members": ["a", "b"], "overview": "offer"}], model="test"
        )
        observed = []

        def record_inputs(inputs):
            observed.append(inputs)
            return fingerprint(inputs)

        monkeypatch.setattr(serve, "fingerprint", record_inputs)
        with closing(serve.connect(Path(storage.db_path))) as reader:
            assert serve._groups(reader, "chosen")[0]["members"] == ["a", "b"]
            assert observed[-1] == rows
            assert serve._groups(reader, "other") == []
            with closing(sqlite3.connect(storage.db_path)) as writer:
                writer.execute("UPDATE analyses SET summary = 'unrelated edit' WHERE goal_id = 'other'")
                writer.commit()
                assert serve._groups(reader, "chosen")
                writer.execute("UPDATE analyses SET extracted_json = '{}' WHERE analysis_id = 'a'")
                writer.commit()
            assert serve._groups(reader, "chosen") == []
    finally:
        await storage.close()


async def test_grouping_failure_retains_results():
    from crawlme.scheduler.engine import CrawlScheduler

    scheduler = object.__new__(CrawlScheduler)
    scheduler._cfg = Settings(_env_file=None)
    scheduler._grouper = MagicMock()
    scheduler._grouper.group = AsyncMock(side_effect=ValueError("bad model reply"))
    scheduler._analysis = MagicMock()
    scheduler._storage = MagicMock()

    async def inputs(goal_id):
        return [{"analysis_id": "a"}]

    scheduler._storage.dedup_inputs = inputs
    scheduler._storage.save_groups = AsyncMock()
    await scheduler._deduplicate(CrawlGoal(prompt="gifts"))
    assert scheduler._dedup_report == {"status": "failed"}
    scheduler._storage.save_groups.assert_not_called()


@pytest.mark.parametrize("enabled", [True, False])
async def test_factory_switch_and_reasoning_default(tmp_path, enabled):
    cfg = Settings(
        _env_file=None, llm_api_key="test", llm_base_url="", llm_dedup_reasoning_effort="off", result_dir=tmp_path
    )
    scheduler = create_scheduler(cfg, budget=TokenBudget(limit=100), dedup_enabled=enabled)
    if enabled:
        assert scheduler._grouper.client._reasoning_effort == "off"
        assert scheduler._grouper.client._stage == "dedup"
    else:
        assert scheduler._grouper is None
    await scheduler.aclose()


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        '{"groups":[{"members":["a","b"],"overview":"x"},{"members":["b"],"overview":"y"}]}',
        '{"groups":[{"members":["a","invented"],"overview":"x"}]}',
        '{"groups":[{"members":[],"overview":"x"}]}',
    ],
)
async def test_invalid_reply_retried(bad, caplog):
    caplog.set_level("WARNING", logger="crawlme.dedup.grouper")
    client = client_response([{"members": ["a", "b"], "overview": "offer"}])
    good = client.chat.return_value
    client.chat.side_effect = [LLMResponse(bad, 1, 1, "test"), good]
    groups = await Grouper(client, max_chars=10000).group(
        CrawlGoal(prompt="gifts"), [{"analysis_id": "a"}, {"analysis_id": "b"}]
    )
    assert [g.members for g in groups] == [["a", "b"]]
    assert client.chat.await_count == 2
    repair = json.loads(client.chat.call_args.args[0])["repair"]
    assert repair["previous_response"] == bad
    assert repair["error"]
    assert any(bad in r.getMessage() and getattr(r, "file_only", False) for r in caplog.records)


async def test_invalid_retry_stops(caplog):
    client = client_response([{"members": ["a", "a", "unknown"], "overview": "offer"}])
    with pytest.raises(Exception, match=r"duplicate IDs.*a.*unknown IDs.*unknown"):
        await Grouper(client, max_chars=10000).group(
            CrawlGoal(prompt="gifts"), [{"analysis_id": "a"}, {"analysis_id": "b"}]
        )
    assert client.chat.await_count == 2


async def test_repair_respects_budget(monkeypatch):
    from types import SimpleNamespace

    from crawlme.llm import LLMClient, TokenBudgetError

    budget = TokenBudget(limit=10)
    client = LLMClient("test", budget=budget)
    complete = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1),
        )
    )
    monkeypatch.setattr(client, "_complete", complete)
    with pytest.raises(TokenBudgetError):
        await Grouper(client, max_chars=10000).group(
            CrawlGoal(prompt="gifts"), [{"analysis_id": "a"}, {"analysis_id": "b"}]
        )
    complete.assert_awaited_once()
    assert budget.calls == 1


async def test_repair_input_limit():
    client = client_response([])
    client.chat.return_value = LLMResponse("x" * 10000, 1, 1, "test")
    with pytest.raises(Exception, match="exceeds"):
        await Grouper(client, max_chars=10000).group(
            CrawlGoal(prompt="gifts"), [{"analysis_id": "a"}, {"analysis_id": "b"}]
        )
    client.chat.assert_awaited_once()
