"""Every log line must survive being formatted."""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "crawlme"
_LEVELS = {"debug", "info", "warning", "error", "critical"}


def _calls():
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _LEVELS:
                continue
            if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"):
                continue
            fmt = node.args[0] if node.args else None
            if isinstance(fmt, ast.Constant) and isinstance(fmt.value, str):
                yield path.relative_to(SRC), node.lineno, fmt.value, len(node.args) - 1


def test_placeholders_match_their_arguments():
    """A count that does not match raises only when the line is logged,
    which for a rare branch means in front of a user. `%2$d` is not
    Python at all and passes review by looking like C."""
    bad = []
    for path, line, fmt, argc in _calls():
        placeholders = fmt.replace("%%", "").count("%")
        if placeholders != argc:
            bad.append(f"{path}:{line} {fmt!r} takes {placeholders}, given {argc}")
    assert bad == [], "\n".join(bad)


def test_info_carries_no_internal_identifiers():
    """INFO is for the person who typed the command. url_keys and byte
    counts belong to DEBUG, where whoever is debugging will look."""
    bad = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "info" or not isinstance(node.func.value, ast.Name):
                continue
            if node.func.value.id != "logger" or not node.args:
                continue
            fmt = node.args[0]
            if isinstance(fmt, ast.Constant) and isinstance(fmt.value, str):
                for banned in ("url_key=", "bytes=", "task_id=", "batch=", "inflight="):
                    if banned in fmt.value:
                        bad.append(f"{path.relative_to(SRC)}:{node.lineno} {banned!r} in an INFO line")
    assert bad == [], "\n".join(bad)
