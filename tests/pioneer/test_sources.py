from __future__ import annotations

import json

import pytest

from crawlme.pioneer.sources.file import FileSource
from crawlme.pioneer.sources.manual import ManualSource
from crawlme.schemas import CrawlGoal


def _goal() -> CrawlGoal:
    return CrawlGoal(prompt="test")


# -- ManualSource --------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_parses():
    src = ManualSource(["https://a.com", "https://b.com"])
    candidates = await src.discover(_goal())
    assert len(candidates) == 2
    assert candidates[0].url.raw == "https://a.com"
    assert candidates[0].depth == 0


@pytest.mark.asyncio
async def test_manual_empty():
    src = ManualSource(["", "  ", "https://a.com"])
    candidates = await src.discover(_goal())
    assert len(candidates) == 1


# -- FileSource ----------------------------------------------------------


@pytest.mark.asyncio
async def test_file_bare_list(tmp_path):
    path = tmp_path / "seeds.json"
    path.write_text(json.dumps(["https://a.com", "https://b.com"]))
    src = FileSource(str(path))
    candidates = await src.discover(_goal())
    assert len(candidates) == 2


@pytest.mark.asyncio
async def test_file_scope(tmp_path):
    path = tmp_path / "seeds.json"
    path.write_text(json.dumps({"seeds": ["https://a.com"], "allowed_domains": ["a.com", "b.com"]}))
    src = FileSource(str(path))
    candidates = await src.discover(_goal())
    assert len(candidates) == 1
    assert src.allowed_domains == {"a.com", "b.com"}


@pytest.mark.asyncio
async def test_file_invalid(tmp_path):
    path = tmp_path / "seeds.json"
    path.write_text("not json")
    src = FileSource(str(path))
    with pytest.raises(json.JSONDecodeError):
        await src.discover(_goal())


@pytest.mark.asyncio
async def test_file_empty_dict(tmp_path):
    path = tmp_path / "seeds.json"
    path.write_text(json.dumps({}))
    src = FileSource(str(path))
    candidates = await src.discover(_goal())
    assert candidates == []
