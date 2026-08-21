"""A local, read-only window onto what a crawl found.

The crawler already stores every judgement it made and the page text
backing each extracted field.  Until now nothing read it back: the
results existed only as rows, or as whatever ``crawl inspect --export``
printed to a terminal.  This serves them as a page you can filter.

It is deliberately read-only and deliberately local.  Nothing here
writes to a run database, and the server binds to the loopback address
only, because a run database holds whatever a logged-in session could
see and that is nobody else's business.

    python dashboard/serve.py            # then open http://127.0.0.1:8765
    python dashboard/serve.py --port 9000 --results-dir ./results

Reading is done with sqlite3 directly rather than through the storage
layer: that layer is async and owns a write queue, neither of which a
request handler wants, and the queries here are the same joins the
inspect command already makes.
"""

from __future__ import annotations

import argparse
import errno
import json
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

HERE = Path(__file__).parent


def _connect(db: Path) -> sqlite3.Connection:
    """Open a run database read-only, so a browse can never damage a run."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _runs(results_dir: Path) -> list[dict[str, Any]]:
    """One entry per run directory that holds a readable database.

    A run whose database is missing or half-written is skipped rather
    than raised on: the directory is created before the crawl starts, so
    an in-progress or crashed run is a normal thing to find here.
    """
    out: list[dict[str, Any]] = []
    for db in sorted(results_dir.glob("*/db/crawl.db"), reverse=True):
        try:
            con = _connect(db)
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


def _results(results_dir: Path, run: str, goal_id: str | None = None) -> dict[str, Any]:
    """The pages-and-analyses join for one run, with evidence intact.

    Same join the inspect command exports, kept here rather than
    imported because that one is shaped for a terminal and a file, and
    this one has to stay cheap enough to re-request while filtering.
    """
    db = results_dir / run / "db" / "crawl.db"
    if not db.is_file():
        raise FileNotFoundError(run)
    con = _connect(db)
    try:
        goals = [dict(g) for g in con.execute("SELECT * FROM crawl_goals ORDER BY created_at")]
        task = con.execute("SELECT * FROM crawl_tasks ORDER BY start_at DESC LIMIT 1").fetchone()
        chosen = goal_id or (task["goal_id"] if task else "")
        pages = {p["url_key"]: dict(p) for p in con.execute("SELECT * FROM pages")}
        rows = []
        for a in con.execute("SELECT * FROM analyses WHERE goal_id = ? ORDER BY relevance_score DESC", (chosen,)):
            page = pages.get(a["url_key"], {})
            url = json.loads(page.get("url_json") or "{}")
            rows.append(
                {
                    "url": url.get("canonical", ""),
                    "host": url.get("domain", ""),
                    "url_key": a["url_key"],
                    "title": page.get("title") or "",
                    "text": (page.get("plain_text") or page.get("markdown") or "")[:2000],
                    "published_at": page.get("published_at") or "",
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
            "rows": rows,
        }
    finally:
        con.close()


class Handler(SimpleHTTPRequestHandler):
    """Static files from this directory, plus a small read-only API."""

    results_dir = Path("results")

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, directory=str(HERE), **kw)

    def do_GET(self) -> None:  # noqa: N802  (the stdlib spells it this way)
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
