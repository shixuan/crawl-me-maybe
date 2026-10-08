"""Item boundaries, revision replacement and grouping through the stored result path."""

import datetime
import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlme.analysis.analyzer import PageAnalyzer, _parse_analysis
from crawlme.dedup.grouper import Grouper, group_results
from crawlme.llm import LLMError, LLMResponse
from crawlme.schemas import URL, AnalysisResult, CrawlGoal, Page
from crawlme.storage.read import read_results
from crawlme.storage.sqlite import SqliteStorage


def page(text):
    return Page(
        url=URL(raw="https://example.com/list", canonical="https://example.com/list", url_key="list"),
        url_key="list",
        plain_text=text,
    )


def response(entries):
    return {
        "classification": "RELEVANT",
        "relevance_score": 0.9,
        "summary": "Matching entries",
        "items": [
            {
                "summary": text,
                "relevance_score": 0.8,
                "evidence": [text],
                "extracted": {"name": {"value": text, "evidence": text}},
            }
            for text in entries
        ],
    }


@pytest.mark.parametrize(
    "goal_text,entries",
    [
        ("Find software jobs", ["Aster seeks a Python engineer.", "Birch seeks a database engineer."]),
        ("Find compact cameras", ["Aster camera weighs 200g.", "Birch camera weighs 220g."]),
        ("Find compiler articles", ["An introduction to register allocation."]),
        ("Find free workshops", ["Aster hosts a free pottery class.", "Birch hosts a free drawing class."]),
    ],
)
def test_general_item_protocol(goal_text, entries):
    goal = CrawlGoal(prompt=goal_text, extraction_spec={"fields": {"name": "matching entry"}})
    result = _parse_analysis(response(entries), page("\n".join(entries)), goal, model="test", tokens_used=10)
    assert len(result.items) == len(entries)
    assert len({item.item_id for item in result.items}) == len(entries)
    assert [item.summary for item in result.items] == entries
    assert [item.extracted["name"].evidence for item in result.items] == entries
    assert result.classification == "RELEVANT"


@pytest.mark.parametrize("items", [None, {}, ["bad"], [], [{"summary": "invented", "evidence": ["missing"]}]])
def test_invalid_items_rejected(items):
    with pytest.raises(LLMError):
        _parse_analysis(
            {"classification": "RELEVANT", "items": items},
            page("source"),
            CrawlGoal(prompt="find entries"),
            model="test",
            tokens_used=0,
        )


async def test_item_time_correction():
    entries = ["Aster ends May 12, 2027.", "Birch ends June 9, 2027."]
    raw = response(entries)
    raw["items"][0]["time"] = {"ends_on": {"value": "May 12, 2027", "evidence": [entries[0]]}}
    raw["items"][1]["time"] = {"ends_on": {"value": "June 9, 2027", "evidence": ["Missing quote"]}}
    client = MagicMock()
    client.chat = AsyncMock(
        side_effect=[
            LLMResponse(json.dumps(raw), 100, 50, "test"),
            LLMResponse(
                json.dumps({"time": {"ends_on": {"value": "June 9, 2027", "evidence": [entries[1]]}}}), 100, 50, "test"
            ),
        ]
    )
    analyzer = PageAnalyzer(client)
    result = await analyzer.analyze(
        page("\n".join(entries)), CrawlGoal(prompt="find offers", time_policy="offer expiry")
    )
    assert [i.ends_on for i in result.items] == [datetime.date(2027, 5, 12), datetime.date(2027, 6, 9)]
    assert result.tokens_used == 300
    assert client.chat.await_count == 2


async def test_items_replay_and_groups(tmp_path):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
    import serve

    storage = SqliteStorage.create(tmp_path)
    await storage.start()
    goal = CrawlGoal(prompt="find products", extraction_spec={"fields": {"name": "product name"}})
    source = page("Aster camera.\nBirch camera.")
    storage.save_goal(goal.model_dump(mode="json"))
    storage.save_page(source)
    result = _parse_analysis(response(["Aster camera.", "Birch camera."]), source, goal, model="test", tokens_used=5)
    storage.save_analysis(result.model_dump(mode="json"))
    try:
        rows = await storage.dedup_inputs(goal.goal_id)
        assert len(rows) == 2
        assert {row["item_id"] for row in rows} == {i.item_id for i in result.items}
        assert len({row["url"] for row in rows}) == 1
        client = MagicMock()
        client.chat = AsyncMock(return_value=LLMResponse('{"groups": []}', 20, 10, "test"))
        report = await group_results(storage, goal, Grouper(client, max_chars=10000), model="test")
        assert report["groups"] == 2
        stored = read_results(Path(storage.db_path))
        assert len(stored.analyses) == 1 and len(stored.items) == 2
        shown = serve._results(tmp_path, Path(storage.db_path).parent.parent.name, goal.goal_id)
        assert len(shown["rows"]) == 2 and len(shown["groups"]) == 2
        other = Page(
            url=URL(raw="https://example.com/aster", canonical="https://example.com/aster", url_key="aster"),
            url_key="aster",
            plain_text="Aster camera.",
        )
        duplicate = _parse_analysis(response(["Aster camera."]), other, goal, model="test", tokens_used=5)
        storage.save_page(other)
        storage.save_analysis(duplicate.model_dump(mode="json"))
        pair = [result.items[0].item_id, duplicate.items[0].item_id]
        client.chat.return_value = LLMResponse(
            json.dumps({"groups": [{"members": pair, "overview": "Aster camera"}]}), 20, 10, "test"
        )
        report = await group_results(storage, goal, Grouper(client, max_chars=10000), model="test")
        assert report == {"status": "complete", "sources": 2, "items": 3, "groups": 2}
        shown = serve._results(tmp_path, Path(storage.db_path).parent.parent.name, goal.goal_id)
        assert len(shown["rows"]) == 3 and len(shown["groups"]) == 2
        replacement = AnalysisResult(
            page_id=source.page_id, url_key=source.url_key, goal_id=goal.goal_id, classification="IRRELEVANT", items=[]
        )
        storage.save_analysis(replacement.model_dump(mode="json"))
        assert [r["item_id"] for r in await storage.dedup_inputs(goal.goal_id)] == [duplicate.items[0].item_id]
    finally:
        await storage.close()
    stored = read_results(Path(storage.db_path))
    assert [i["item_id"] for i in stored.items] == [duplicate.items[0].item_id]
    assert len(stored.analyses) == 2
    assert serve._results(tmp_path, Path(storage.db_path).parent.parent.name, goal.goal_id)["groups"] == []


async def test_item_write_is_atomic(tmp_path):
    storage = SqliteStorage.create(tmp_path)
    await storage.start()
    data = {"analysis_id": "bad", "items": [{"item_id": "duplicate", "summary": "A"}] * 2}
    storage.save_analysis(data)
    await storage.close()
    with sqlite3.connect(storage.db_path) as con:
        assert con.execute("SELECT count(*) FROM analyses").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM analysis_items").fetchone()[0] == 0
