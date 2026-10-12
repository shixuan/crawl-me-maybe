"""Queries and row shapes shared by live storage and read-only result consumers."""

from __future__ import annotations

import json
from typing import Any

GOALS = "SELECT * FROM crawl_goals ORDER BY created_at"
PAGES = "SELECT * FROM pages ORDER BY extracted_at, page_id"
ANALYSES = "SELECT * FROM analyses ORDER BY analyzed_at"
# Replay appends revisions; readers select one result before filtering verdicts or dates.
LATEST_ANALYSES = (
    "SELECT a.* FROM analyses a WHERE a.rowid = ("
    "SELECT n.rowid FROM analyses n WHERE n.goal_id = a.goal_id AND n.url_key = a.url_key "
    "ORDER BY n.analyzed_at DESC, n.rowid DESC LIMIT 1)"
)
DEDUP_INPUTS = (
    f"SELECT a.*, p.url_json, p.published_at FROM ({LATEST_ANALYSES}) a "  # noqa: S608 — static SQL only
    "JOIN pages p ON p.page_id = a.page_id WHERE a.goal_id = ? "
    "AND a.classification = 'RELEVANT' ORDER BY a.analysis_id"
)


def items_query(*, modern: bool = True) -> str:
    """Expand relevant items; retain page verdicts and legacy analyses as single rows."""
    legacy = f"SELECT a.*, a.analysis_id AS item_id, '[]' AS evidence_json FROM ({LATEST_ANALYSES}) a"  # noqa: S608
    if not modern:
        return legacy
    return (
        "SELECT a.analysis_id, a.page_id, a.url_key, a.goal_id, a.classification, "  # noqa: S608
        "i.relevance_score, i.starts_on, i.ends_on, i.summary, a.structured_data, "
        "i.extracted_json, i.tags_json, a.feedback_json, a.model, a.prompt_version, "
        "a.spec_version, a.tokens_used, a.analyzed_at, a.item_count, i.item_id, i.evidence_json "
        f"FROM ({LATEST_ANALYSES}) a JOIN analysis_items i ON i.analysis_id = a.analysis_id "
        "WHERE a.classification = 'RELEVANT' UNION ALL "
        + legacy
        + " WHERE a.item_count IS NULL OR a.classification != 'RELEVANT'"
    )


def item_dedup_query(*, modern: bool = True) -> str:
    return (
        f"SELECT a.*, p.url_json, p.published_at FROM ({items_query(modern=modern)}) a "  # noqa: S608
        "JOIN pages p ON p.page_id = a.page_id WHERE a.goal_id = ? "
        "AND a.classification = 'RELEVANT' ORDER BY a.item_id"
    )


def dedup_input(row: dict[str, Any]) -> dict[str, Any]:
    """The stored evidence used to group a relevant analysis and detect stale groups."""
    return {
        "item_id": row.get("item_id", row["analysis_id"]),
        "url": json.loads(row["url_json"])["canonical"],
        "published_at": row.get("published_at"),
        "summary": row.get("summary"),
        "extracted": json.loads(row.get("extracted_json") or "{}"),
        "starts_on": row.get("starts_on") or "",
        "ends_on": row.get("ends_on") or "",
        "relevance": row["relevance_score"],
        "evidence": json.loads(row.get("evidence_json") or "[]"),
    }
