"""Queries and row shapes shared by live storage and read-only result consumers."""

from __future__ import annotations

import json
from typing import Any

GOALS = "SELECT * FROM crawl_goals ORDER BY created_at"
PAGES = "SELECT * FROM pages ORDER BY extracted_at, page_id"
ANALYSES = "SELECT * FROM analyses ORDER BY analyzed_at"
DEDUP_INPUTS = (
    "SELECT a.*, p.url_json, p.published_at FROM analyses a "
    "JOIN pages p ON p.page_id = a.page_id WHERE a.goal_id = ? "
    "AND a.classification = 'RELEVANT' ORDER BY a.analysis_id"
)


def dedup_input(row: dict[str, Any]) -> dict[str, Any]:
    """The stored evidence used to group a relevant analysis and detect stale groups."""
    return {
        "analysis_id": row["analysis_id"],
        "url": json.loads(row["url_json"])["canonical"],
        "published_at": row.get("published_at"),
        "summary": row.get("summary"),
        "extracted": json.loads(row.get("extracted_json") or "{}"),
        "starts_on": row.get("starts_on") or "",
        "ends_on": row.get("ends_on") or "",
        "relevance": row["relevance_score"],
    }
