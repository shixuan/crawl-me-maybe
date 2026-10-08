"""Serve stored crawl results read-only on loopback.

Run with python dashboard/serve.py [--port 8765] [--results-dir results]."""

from __future__ import annotations

import argparse
import errno
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from crawlme.dedup.grouper import fingerprint
from crawlme.schemas.analysis import CLASSIFICATIONS
from crawlme.storage import queries
from crawlme.storage.read import connect, has_items, read_items
from crawlme.util.dates import group_of

HERE = Path(__file__).parent


def _runs(results_dir: Path) -> list[dict[str, Any]]:
    """List runs with readable databases, skipping incomplete or missing task records."""
    out: list[dict[str, Any]] = []
    for db in sorted(results_dir.glob("*/db/crawl.db"), reverse=True):
        try:
            con = connect(db)
        except sqlite3.Error:
            continue
        try:
            task = con.execute("SELECT * FROM crawl_tasks ORDER BY start_at DESC LIMIT 1").fetchone()
            if task is None:
                continue
            goal = con.execute("SELECT * FROM crawl_goals WHERE goal_id = ?", (task["goal_id"],)).fetchone()
            counts = dict(con.execute("SELECT classification, COUNT(*) FROM analyses GROUP BY 1").fetchall())
            counters = json.loads(task["counters"] or "{}")
            out.append(
                {
                    "run": db.parent.parent.name,
                    "task_id": task["task_id"],
                    "state": task["state"],
                    "reason": task["stopping_reason"] or "",
                    "started": task["start_at"],
                    "ended": task["end_at"] or "",
                    "prompt": (goal["prompt"] if goal else ""),
                    "pages": counters.get("pages_fetched", 0),
                    "tokens": counters.get("tokens_used", 0),
                    "counts": counts,
                    # Whether a run produced anything is a question about
                    # analyses, not about one class of them: a goal that
                    # never uses RELEVANT is a goal, not an empty run.
                    "analyses": sum(counts.values()),
                }
            )
        except (sqlite3.Error, json.JSONDecodeError):
            continue
        finally:
            con.close()
    return out


def _day(raw: Any) -> date | None:
    """Dates come back from a run database as ISO text, or as nothing."""
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def _results(results_dir: Path, run: str, goal_id: str | None = None) -> dict[str, Any]:
    """Join a goal analyses with page content, evidence and event dates."""
    db = results_dir / run / "db" / "crawl.db"
    if not db.is_file():
        raise FileNotFoundError(run)
    con = connect(db)
    try:
        con.execute("BEGIN")
        goals = [dict(g) for g in con.execute(queries.GOALS)]
        task = con.execute("SELECT * FROM crawl_tasks ORDER BY start_at DESC LIMIT 1").fetchone()
        chosen = goal_id or (task["goal_id"] if task else "")
        page_rows = [dict(p) for p in con.execute("SELECT * FROM pages")]
        pages = {p["url_key"]: p for p in page_rows}
        pages_by_id = {p["page_id"]: p for p in page_rows if p.get("page_id")}
        # No horizon here. That line is a knob on the page, and moving
        # it needs no new data.
        today = datetime.now(timezone.utc).date()
        rows = []
        for row in sorted(read_items(con), key=lambda r: -r["relevance_score"]):
            if row["goal_id"] != chosen:
                continue
            # A run from before the dates were stored has no such
            # columns. A dict reads those as blank instead of raising.
            a = dict(row)
            page = pages_by_id.get(a.get("page_id")) or pages.get(a["url_key"], {})
            url = json.loads(page.get("url_json") or "{}")
            rows.append(
                {
                    "analysis_id": a.get("analysis_id", a["url_key"]),
                    "item_id": a["item_id"],
                    "evidence": json.loads(a.get("evidence_json") or "[]"),
                    "url": url.get("canonical", ""),
                    "host": url.get("domain", ""),
                    "url_key": a["url_key"],
                    "title": page.get("title") or "",
                    "text": (page.get("plain_text") or page.get("markdown") or "")[:2000],
                    "published_at": page.get("published_at") or "",
                    "starts_on": a.get("starts_on") or "",
                    "ends_on": a.get("ends_on") or "",
                    "when": group_of(_day(a.get("starts_on")), _day(a.get("ends_on")), today),
                    "classification": a["classification"],
                    "relevance": a["relevance_score"],
                    "summary": a["summary"] or "",
                    "tags": json.loads(a["tags_json"] or "[]"),
                    "extracted": json.loads(a["extracted_json"] or "{}"),
                    "model": a["model"],
                    "analyzed_at": a["analyzed_at"],
                }
            )
        spec = next((json.loads(g["extraction_spec"] or "{}") for g in goals if g["goal_id"] == chosen), {})
        return {
            "run": run,
            "goal_id": chosen,
            "goals": [{"goal_id": g["goal_id"], "prompt": g["prompt"]} for g in goals],
            "fields": list((spec or {}).get("fields", {}).keys()),
            "time_enabled": any(g.get("time_policy") for g in goals if g["goal_id"] == chosen)
            or bool((spec or {}).get("time_field"))
            or any(r["starts_on"] or r["ends_on"] for r in rows),
            # Declared order, not order of arrival: the verdicts read the
            # same way every run whatever the counts happen to be.
            "classifications": list(CLASSIFICATIONS),
            "rows": rows,
            "groups": _groups(con, chosen),
        }
    finally:
        con.close()


def _groups(con: sqlite3.Connection, goal_id: str) -> list[dict[str, Any]]:
    """Read persisted groups only when the complete relevant input still matches."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dedup_runs'").fetchone():
        return []
    run = con.execute(
        "SELECT * FROM dedup_runs WHERE goal_id = ? ORDER BY created_at DESC LIMIT 1", (goal_id,)
    ).fetchone()
    if run is None:
        return []
    modern = run["version"] == "items"
    query = queries.item_dedup_query(modern=has_items(con)) if modern else queries.DEDUP_INPUTS
    inputs = [queries.dedup_input(dict(row)) for row in con.execute(query, (goal_id,))]
    if not modern:
        for entry in inputs:
            entry["analysis_id"] = entry.pop("item_id")
            entry.pop("evidence")
    if fingerprint(inputs) != run["fingerprint"]:
        return []
    groups = []
    for group in con.execute("SELECT * FROM result_groups WHERE dedup_id = ? ORDER BY rowid", (run["dedup_id"],)):
        membership = (
            "SELECT item_id FROM result_item_members WHERE group_id = ? ORDER BY rowid"
            if modern
            else "SELECT analysis_id FROM result_members WHERE group_id = ? ORDER BY rowid"
        )
        members = [r[0] for r in con.execute(membership, (group["group_id"],))]
        groups.append({"group_id": group["group_id"], "overview": group["overview"], "members": members})
    return groups


class Handler(SimpleHTTPRequestHandler):
    """Static files from this directory, plus a small read-only API."""

    results_dir = Path("results")

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, directory=str(HERE), **kw)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            super().do_GET()
            return
        try:
            if path == "/api/runs":
                self._json({"runs": _runs(self.results_dir)})
            elif path.startswith("/api/run/"):
                parts = [unquote(p) for p in path[len("/api/run/") :].split("/") if p]
                self._json(_results(self.results_dir, parts[0], parts[1] if len(parts) > 1 else None))
            else:
                self._json({"error": "no such endpoint"}, status=404)
        except FileNotFoundError:
            self._json({"error": "no such run"}, status=404)
        except (sqlite3.Error, json.JSONDecodeError, IndexError) as e:
            self._json({"error": str(e)}, status=500)

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Results are re-read on every request: a run that is still
        # going should not be shown from a cache that predates it.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        sys.stderr.write(f"{stamp} {fmt % args}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results-dir", default="results", help="Where run directories live (default: results)")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    results = Path(args.results_dir)
    if not results.is_dir():
        print(f"no results directory at {results}", file=sys.stderr)
        return 2
    Handler.results_dir = results

    n = len(_runs(results))
    # Loopback only.  A run database holds whatever a logged-in session
    # could see, so this is not something to expose on a network.
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        # Almost always this same dashboard, still running in another
        # terminal.  A traceback here says nothing a reader can act on.
        print(
            f"port {args.port} is already taken -- another dashboard is probably still running.\n"
            f"  open http://127.0.0.1:{args.port} to use it,\n"
            f"  or start this one elsewhere:  --port {args.port + 1}",
            file=sys.stderr,
        )
        return 2
    print(f"dashboard on http://127.0.0.1:{args.port}  ({n} runs in {results})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
