"""Session files are validated and merged before browser startup."""

import json
from pathlib import Path

import pytest

from crawlme.errors import Error
from crawlme.sessions import load_sessions


def test_state_loads(tmp_path: Path) -> None:
    p = tmp_path / "session.json"
    p.write_text(json.dumps({"cookies": [{"name": "sessionid", "value": "x"}], "origins": []}))
    assert load_sessions([str(p)])["cookies"][0]["name"] == "sessionid"


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (None, "not found"),
        ("not json at all", "readable JSON"),
        (json.dumps({"cookies": [], "origins": []}), "no cookies"),
    ],
)
def test_state_bad_path(tmp_path: Path, content, match) -> None:
    """Silently falling back to anonymous would crawl a login wall.

    On a login-walled platform that means a few hundred fetches of the
    same sign-in page and a conclusion that the site is empty.
    """
    p = tmp_path / "session.json"
    if content is not None:
        p.write_text(content)
    with pytest.raises(Error, match=match):
        load_sessions([str(p)])


def test_sessions_merge_in_order_without_changing_files(tmp_path):
    cookie = {"name": "session", "value": "old", "domain": ".reddit.com", "path": "/"}
    first = {
        "cookies": [cookie, {**cookie, "partitionKey": "https://example.com"}],
        "origins": [
            {
                "origin": "https://www.reddit.com",
                "localStorage": [{"name": "token", "value": "old"}, {"name": "keep", "value": "yes"}],
            }
        ],
    }
    second = {
        "cookies": [{**cookie, "value": "new"}, {**cookie, "domain": ".instagram.com"}, {**cookie, "path": "/other"}],
        "origins": [
            {"origin": "https://www.reddit.com", "localStorage": [{"name": "token", "value": "new"}]},
            {"origin": "https://www.instagram.com", "localStorage": [{"name": "token", "value": "ig"}]},
        ],
    }
    paths = [tmp_path / "first.json", tmp_path / "second.json"]
    contents = [json.dumps(first), json.dumps(second)]
    for path, content in zip(paths, contents):
        path.write_text(content)
    result = load_sessions([str(path) for path in paths])
    assert len(result["cookies"]) == 4
    assert result["cookies"][0]["value"] == "new"
    assert result["cookies"][1]["partitionKey"] == "https://example.com"
    assert result["cookies"][1]["value"] == "old"
    assert result["origins"][0]["localStorage"] == [{"name": "token", "value": "new"}, {"name": "keep", "value": "yes"}]
    assert result["origins"][1] == second["origins"][1]
    assert [path.read_text() for path in paths] == contents


@pytest.mark.parametrize(
    "bad", ["not json", '{"cookies": [null]}', '{"origins": [{"origin": "https://example.com", "localStorage": null}]}']
)
def test_multiple_sessions_reject_invalid_file(tmp_path, bad):
    good = tmp_path / "good.json"
    good.write_text('{"cookies": [{"name": "s", "value": "ok"}]}')
    invalid = tmp_path / "invalid.json"
    invalid.write_text(bad)
    with pytest.raises(Error, match=r"invalid\.json"):
        load_sessions([str(good), str(invalid)])


def test_multiple_sessions_reject_missing_file(tmp_path):
    good = tmp_path / "good.json"
    good.write_text('{"cookies": [{"name": "s", "value": "ok"}]}')
    with pytest.raises(Error, match=r"missing\.json"):
        load_sessions([str(good), str(tmp_path / "missing.json")])
