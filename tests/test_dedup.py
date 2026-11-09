"""Validate grouping boundaries, persistence and scheduler failure isolation."""

import json
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.analysis import AnalysisResult
from crawlme.dedup.grouper import DedupError, Grouper, fingerprint
from crawlme.digest import Page
from crawlme.errors import Error, ErrorHandler, Kind
from crawlme.llm import LLMResponse, TokenBudget
from crawlme.persist import Storage
from crawlme.runtime import Goal
from crawlme.scheduler.engine import create_engine
from crawlme.urls import URL


def client_response(groups, *, singletons=(), truncated=False):
    client = MagicMock()
    client.chat = AsyncMock(
        return_value=LLMResponse(
            json.dumps({"groups": groups, "singletons": list(singletons)}), 100, 20, "test", truncated
        )
    )
    return client


@pytest.mark.parametrize(
    "groups",
    [
        [{"members": ["1", "1", "2"], "overview": "duplicate"}],
        [{"members": ["1", "2", "invented"], "overview": "unknown"}],
        [{"members": [], "overview": "empty"}],
        [{"members": ["1"], "overview": "singleton in groups"}],
    ],
)
async def test_invalid_partition_rejected(groups):
    grouper = Grouper(client_response(groups), max_chars=10000)
    with pytest.raises(DedupError) as caught:
        await grouper.group(Goal(prompt="gifts"), [{"analysis_id": "a"}, {"analysis_id": "b"}])
    assert isinstance(caught.value, Error)
    assert caught.value.source == "dedup" and caught.value.kind == Kind.INVALID_OUTPUT
    if len(groups[0]["members"]) < 2:
        assert caught.value.code == "invalid_groups" and isinstance(caught.value.__cause__, ValueError)
    else:
        assert caught.value.code == "invalid_members"


async def test_group_preserves_evidence_and_does_not_output_a_score():
    client = client_response([{"members": ["1", "2"], "overview": "Same offer; terms differ."}])
    rows = [
        {"analysis_id": "a", "extracted": {"condition": {"value": "two large", "evidence": "two large"}}},
        {"analysis_id": "b", "extracted": {"condition": {"value": "any two", "evidence": "any two"}}},
    ]
    result = await Grouper(client, max_chars=10000).group(Goal(prompt="gifts"), rows)
    assert result[0].members == ["a", "b"]
    assert json.loads(client.chat.call_args.args[0])["results"] == [
        {"ref": "1", "extracted": rows[0]["extracted"]},
        {"ref": "2", "extracted": rows[1]["extracted"]},
    ]
    assert set(result[0].model_dump()) == {"members", "overview"}


async def test_no_truncation_or_call_for_oversized_input():
    client = client_response([])
    with pytest.raises(DedupError, match="exceeds"):
        await Grouper(client, max_chars=10).group(Goal(prompt="gifts"), [{"analysis_id": "a"}, {"analysis_id": "b"}])
    client.chat.assert_not_called()


async def test_truncated_output_rejected():
    client = client_response([{"members": ["1", "2"], "overview": "offer"}], truncated=True)
    with pytest.raises(DedupError, match="truncated"):
        await Grouper(client, max_chars=10000).group(Goal(prompt="gifts"), [{"analysis_id": "a"}, {"analysis_id": "b"}])


async def test_singleton_needs_no_llm():
    client = client_response([])
    groups = await Grouper(client, max_chars=10000).group(
        Goal(prompt="gifts"), [{"analysis_id": "a", "summary": "offer"}]
    )
    assert groups[0].members == ["a"]
    client.chat.assert_not_called()


async def test_explicit_singletons_reuse_original_summaries():
    rows = [{"analysis_id": "a", "summary": "one"}, {"analysis_id": "b", "summary": "two"}]
    groups = await Grouper(client_response([], singletons=["1", "2"]), max_chars=10000).group(
        Goal(prompt="gifts"), rows
    )
    assert [g.members for g in groups] == [["a"], ["b"]]
    assert [g.overview for g in groups] == ["one", "two"]


@pytest.mark.parametrize("singletons", [[], ["1"]])
async def test_missing_references_rejected(singletons):
    client = client_response([], singletons=singletons)
    with pytest.raises(DedupError) as caught:
        await Grouper(client, max_chars=10000).group(Goal(prompt="gifts"), [{"item_id": "a"}, {"item_id": "b"}])
    assert caught.value.code == "invalid_members"
    assert "2" in caught.value.details["missing"]
    assert client.chat.await_count == 2


@pytest.mark.parametrize(
    ("groups", "singletons", "error"),
    [
        ([{"members": ["1", "2"], "overview": "same"}], ["2"], "duplicates"),
        ([], ["1", "1", "2"], "duplicates"),
        ([], ["1", "2", "3"], "unknown"),
    ],
)
async def test_references_checked_across_groups_and_singletons(groups, singletons, error):
    client = client_response(groups, singletons=singletons)
    with pytest.raises(DedupError) as caught:
        await Grouper(client, max_chars=10000).group(Goal(prompt="gifts"), [{"item_id": "a"}, {"item_id": "b"}])
    assert caught.value.code == "invalid_members"
    assert caught.value.details[error]


async def test_references_are_stable_and_do_not_replace_storage_ids():
    rows = [
        {"item_id": "z-real", "analysis_id": "parent-z", "summary": "unique"},
        {"item_id": "b-real", "analysis_id": "parent-b", "summary": "second"},
        {"item_id": "a-real", "analysis_id": "parent-a", "summary": "first"},
    ]
    before = json.dumps(rows)
    before_fingerprint = fingerprint(rows)
    client = client_response([{"members": ["1", "2"], "overview": "same"}], singletons=["3"])
    grouper = Grouper(client, max_chars=10000)
    groups = await grouper.group(Goal(prompt="gifts"), rows)
    first_prompt = client.chat.call_args.args[0]
    again = await grouper.group(Goal(prompt="gifts"), list(reversed(rows)))
    assert client.chat.call_args.args[0] == first_prompt
    assert json.loads(first_prompt)["results"] == [
        {"ref": "1", "summary": "first"},
        {"ref": "2", "summary": "second"},
        {"ref": "3", "summary": "unique"},
    ]
    assert [g.members for g in groups] == [["a-real", "b-real"], ["z-real"]]
    assert groups == again
    assert json.dumps(rows) == before
    assert fingerprint(rows) == before_fingerprint


async def test_duplicate_input_ids_rejected_before_call():
    client = client_response([], singletons=["1", "2"])
    with pytest.raises(DedupError) as caught:
        await Grouper(client, max_chars=10000).group(Goal(prompt="gifts"), [{"item_id": "a"}, {"item_id": "a"}])
    assert caught.value.kind == Kind.INVALID_INPUT
    client.chat.assert_not_called()


async def test_persisted_groups_dashboard_and_stale_replay(tmp_path):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
    import serve

    storage = Storage.create(tmp_path)
    await storage.start()
    goal = Goal(prompt="gifts")
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
        ids = [r["item_id"] for r in rows]
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
            assert con.execute("SELECT count(*) FROM dedup_snapshots").fetchone()[0] == 1
            assert con.execute("SELECT count(*) FROM page_analyses").fetchone()[0] == 2
            con.execute("UPDATE page_analyses SET summary = 'changed after replay'")
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

    storage = Storage.create(tmp_path)
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
        assert [row["item_id"] for row in rows] == ["a"]
        assert [row["extracted"]["offer"]["value"] for row in rows] == ["a"]
        await storage.save_groups("chosen", fingerprint(rows), [{"members": ["a"], "overview": "offer"}], model="test")
        observed = []

        def record_inputs(inputs):
            observed.append(inputs)
            return fingerprint(inputs)

        monkeypatch.setattr(serve, "fingerprint", record_inputs)
        with closing(serve.connect(Path(storage.db_path))) as reader:
            assert serve._groups(reader, "chosen")[0]["members"] == ["a"]
            assert observed[-1] == rows
            assert serve._groups(reader, "other") == []
            with closing(sqlite3.connect(storage.db_path)) as writer:
                writer.execute("UPDATE page_analyses SET summary = 'unrelated edit' WHERE goal_id = 'other'")
                writer.commit()
                assert serve._groups(reader, "chosen")
                writer.execute("UPDATE page_analyses SET summary = 'old revision edit' WHERE analysis_id = 'b'")
                writer.commit()
                assert serve._groups(reader, "chosen")
                writer.execute("UPDATE page_analyses SET extracted_json = '{}' WHERE analysis_id = 'a'")
                writer.commit()
            assert serve._groups(reader, "chosen") == []
    finally:
        await storage.close()


async def test_grouping_failure_retains_results():
    from crawlme.dedup import DedupError
    from crawlme.scheduler.pipelines.dedup import DedupPipeline

    grouper = MagicMock(group=AsyncMock(side_effect=DedupError("bad model reply")))
    storage = MagicMock(dedup_inputs=AsyncMock(return_value=[{"analysis_id": "a"}]), save_groups=AsyncMock())
    worker = DedupPipeline(grouper, storage=storage, model="test", error_handler=ErrorHandler())
    await create_engine(worker).run(Goal(prompt="gifts"))
    assert worker.report == {"status": "failed", "error": "bad model reply"}
    storage.save_groups.assert_not_called()


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        '{"groups":[{"members":["1","2"],"overview":"x"}],"singletons":["2"]}',
        '{"groups":[{"members":["1","invented"],"overview":"x"}],"singletons":[]}',
        '{"groups":[{"members":[],"overview":"x"}],"singletons":["1","2"]}',
        '{"groups":[],"singletons":["1"]}',
        '{"groups":[]}',
        '{"singletons":["1","2"]}',
        '{"groups":[],"singletons":[1,2]}',
    ],
)
async def test_invalid_reply_retried(bad, caplog):
    caplog.set_level("WARNING", logger="crawlme.dedup.grouper")
    client = client_response([{"members": ["1", "2"], "overview": "offer"}])
    good = client.chat.return_value
    client.chat.side_effect = [LLMResponse(bad, 1, 1, "test"), good]
    groups = await Grouper(client, max_chars=10000).group(
        Goal(prompt="gifts"), [{"analysis_id": "a"}, {"analysis_id": "b"}]
    )
    assert [g.members for g in groups] == [["a", "b"]]
    assert client.chat.await_count == 2
    repair = json.loads(client.chat.call_args.args[0])["repair"]
    assert repair["previous_response"] == bad
    assert repair["error"]
    assert any(bad in r.getMessage() and getattr(r, "file_only", False) for r in caplog.records)


async def test_invalid_retry_stops(caplog):
    client = client_response([{"members": ["1", "1", "unknown"], "overview": "offer"}])
    with pytest.raises(DedupError, match=r"duplicate refs.*1.*unknown refs.*unknown.*missing refs.*2"):
        await Grouper(client, max_chars=10000).group(Goal(prompt="gifts"), [{"analysis_id": "a"}, {"analysis_id": "b"}])
    assert client.chat.await_count == 2


async def test_repair_respects_budget(monkeypatch):
    from types import SimpleNamespace

    from crawlme.llm import Client, TokenBudgetError

    budget = TokenBudget(limit=10)
    client = Client("test", budget=budget)
    complete = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1),
        )
    )
    monkeypatch.setattr(client, "_complete", complete)
    with pytest.raises(TokenBudgetError):
        await Grouper(client, max_chars=10000).group(Goal(prompt="gifts"), [{"analysis_id": "a"}, {"analysis_id": "b"}])
    complete.assert_awaited_once()
    assert budget.calls == 1


async def test_repair_input_limit():
    client = client_response([])
    client.chat.return_value = LLMResponse("x" * 10000, 1, 1, "test")
    with pytest.raises(DedupError, match="exceeds"):
        await Grouper(client, max_chars=10000).group(Goal(prompt="gifts"), [{"analysis_id": "a"}, {"analysis_id": "b"}])
    client.chat.assert_awaited_once()


async def test_latest_verdict_before_filter(tmp_path):
    storage = Storage.create(tmp_path)
    await storage.start()
    try:
        page = Page(url=URL(raw="https://example.com", canonical="https://example.com", url_key="page"), url_key="page")
        storage.save_page(page)
        for key, verdict, stamp in [
            ("old", "RELEVANT", "2026-01-01"),
            ("new", "IRRELEVANT", "2026-01-02"),
            ("late-insert", "RELEVANT", "2026-01-01"),
        ]:
            storage.save_analysis(
                {
                    "analysis_id": key,
                    "page_id": page.page_id,
                    "url_key": page.url_key,
                    "goal_id": "goal",
                    "classification": verdict,
                    "analyzed_at": stamp,
                }
            )
        assert await storage.dedup_inputs("goal") == []
    finally:
        await storage.close()


async def test_compact_input_boundary():
    from crawlme import prompts

    goal = Goal(prompt="offers")
    rows = [{"analysis_id": "a", "summary": "A café offer"}, {"analysis_id": "b"}]
    payload = {
        "goal": goal.prompt,
        "spec": goal.extraction_spec,
        "results": [{"summary": "A café offer", "ref": "1"}, {"ref": "2"}],
    }
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    limit = len(compact) + len(prompts.DEDUP_SYSTEM)
    client = client_response([], singletons=["1", "2"])
    await Grouper(client, max_chars=limit).group(goal, rows)
    assert client.chat.call_args.args[0] == compact
    client.chat.reset_mock()
    with pytest.raises(DedupError, match="exceeds"):
        await Grouper(client, max_chars=limit - 1).group(goal, rows)
    client.chat.assert_not_called()


async def test_group_storage_failure_is_not_retried():
    from crawlme.scheduler.pipelines.dedup import DedupPipeline

    failure = OSError("disk full")
    grouper = MagicMock(group=AsyncMock(return_value=[]))
    storage = MagicMock(dedup_inputs=AsyncMock(return_value=[]), save_groups=AsyncMock(side_effect=failure))
    worker = DedupPipeline(grouper, storage=storage, model="test", error_handler=ErrorHandler())
    with pytest.raises(OSError) as caught:
        await create_engine(worker).run(Goal(prompt="gifts"))
    assert caught.value is failure
    grouper.group.assert_awaited_once()
    storage.save_groups.assert_awaited_once()
