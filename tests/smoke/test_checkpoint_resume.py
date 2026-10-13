"""Resume a real CLI process with page extraction and scripted LLM stages."""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

# Replace only the provider transport. Client admission and token accounting remain real.
_CLI = """
from types import SimpleNamespace
import httpx
from crawlme.llm.client import LLMClient
from crawlme.cli import main

async def complete(self, messages, max_tokens, json_mode, effort=""):
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        response = await client.post(self._base_url, json={"messages": messages})
        response.raise_for_status()
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=response.text))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )

LLMClient._complete = complete
main()
"""


@pytest.mark.parametrize("legacy", [False, True], ids=["current-db", "legacy-db"])
@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGKILL])
def test_checkpoint_resume(tmp_path, interrupt, legacy):
    entered, release = threading.Event(), threading.Event()
    requests, calls = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            link = '<a href="/second">Compiler optimization techniques</a>' if self.path == "/first" else ""
            self.send_body(
                (
                    "<html><head><title>Compiler design</title></head><body><article>"
                    + "<p>Compiler design involves parsing, optimization and code generation.</p>" * 20
                    + link
                    + "</article></body></html>"
                ).encode(),
                "text/html",
            )

        def do_POST(self):
            messages = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["messages"]
            prompt = messages[-1]["content"]
            if "## Candidate links" in prompt:
                calls.append("ranking")
                body = {
                    "rankings": [
                        {"id": name, "priority": 0.9, "rationale": "compiler tooling"}
                        for name in re.findall(r"^(c\d+):", prompt, flags=re.MULTILINE)
                    ],
                    "candidates_to_drop": [],
                }
            elif "## Page\n" in prompt:
                page = prompt.split("## Page\n", 1)[1].splitlines()[0]
                path = urlsplit(page).path
                calls.append(path)
                if path == "/second":
                    entered.set()
                    if not release.wait(20):
                        self.send_error(504)
                        return
                body = {
                    "classification": "RELEVANT",
                    "relevance_score": 0.9,
                    "items": [
                        {
                            "summary": f"Compiler design at {path}",
                            "relevance_score": 0.9,
                            "evidence": ["Compiler design involves parsing, optimization and code generation."],
                        }
                    ],
                }
            else:
                calls.append("goal")
                body = {"goal_statement": "compiler design", "keywords": ["compiler", "design"]}
            self.send_body(json.dumps(body).encode(), "application/json")

        def send_body(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    site = f"http://127.0.0.1:{server.server_port}"
    env = {
        **os.environ,
        "LLM_API_KEY": "",
        "LLM_BASE_URL": site + "/llm",
        "LLM_MODEL": "scripted",
        "LLM_CONCURRENCY": "1",
        "FETCH_CONCURRENCY": "1",
        "ANALYSIS_ENABLED": "true",
        "FETCHER": "http",
        "EXPAND_SEEDS": "false",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "NO_PROXY": "127.0.0.1",
    }
    command = [sys.executable, "-c", _CLI]
    root = tmp_path / "results"
    process = None
    try:
        process = subprocess.Popen(  # noqa: S603
            [
                *command,
                "run",
                "compiler design",
                "--seeds",
                site + "/first",
                "--dedup",
                "off",
                "--ignore-robots",
                "--max-pages",
                "0",
                "--max-tokens",
                "300",
                "--max-duration",
                "60",
                "--result-dir",
                str(root),
                "--log-level",
                "WARNING",
            ],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert entered.wait(15), "crawl did not reach the second page's analysis"
        db_path = next(root.glob("*/db/crawl.db"))
        with sqlite3.connect(db_path) as db:
            task_id, goal_id = db.execute("SELECT task_id, goal_id FROM crawl_tasks").fetchone()
            saved = json.loads(db.execute("SELECT checkpoint_json FROM checkpoints").fetchone()[0])
            first_analysis = db.execute("SELECT analysis_id, structured_data FROM analyses").fetchone()
        assert first_analysis is not None
        assert saved["state"]["progress"]["pages_fetched"] == 1
        assert saved["budget"]["used"] == 45
        assert saved["budget"]["by_stage"]["analysis"]["calls"] == 1
        assert saved["goal"]["max_tokens"] == 300
        assert len(saved["work"]) == 1
        assert saved["work"][0]["page"]["url"]["canonical"] == site + "/second"
        assert not saved["work"][0].get("analysis_done")
        assert saved["ranking"] and saved["analysis"]
        before = list(calls)

        process.send_signal(interrupt)
        output, _ = process.communicate(timeout=10)
        assert process.returncode == (130 if interrupt == signal.SIGINT else -signal.SIGKILL), output
        assert calls == before, "interruption must not start another LLM call"
        if interrupt == signal.SIGINT:
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT state FROM crawl_tasks").fetchone()[0] == "INTERRUPTED"

        if legacy:
            with sqlite3.connect(db_path) as db:
                db.execute("ALTER TABLE checkpoints RENAME TO frontier_snapshots")
                db.execute("ALTER TABLE frontier_snapshots RENAME COLUMN checkpoint_id TO snapshot_id")
                db.execute("ALTER TABLE frontier_snapshots RENAME COLUMN checkpoint_json TO snapshot_json")

        release.set()
        resumed = subprocess.run(  # noqa: S603
            [*command, "resume", task_id, "--result-dir", str(root)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        assert requests.count("/first") == requests.count("/second") == 1
        assert calls.count("goal") == calls.count("ranking") == calls.count("/first") == 1
        assert calls.count("/second") == 2
        assert len(list(root.glob("*/db/crawl.db"))) == 1
        with sqlite3.connect(db_path) as db:
            tasks = db.execute("SELECT task_id, goal_id, state, counters FROM crawl_tasks").fetchall()
            assert len(tasks) == 1
            restored_id, restored_goal, state, counters = tasks[0]
            assert (restored_id, restored_goal, state) == (task_id, goal_id, "COMPLETED")
            assert json.loads(counters) == {"pages_fetched": 2, "tokens_used": 60}
            assert db.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 2
            assert db.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 2
            assert (
                db.execute(
                    "SELECT analysis_id, structured_data FROM analyses WHERE analysis_id = ?", (first_analysis[0],)
                ).fetchone()
                == first_analysis
            )
            assert db.execute("SELECT COUNT(*) FROM rank_decisions WHERE ranker = 'llm'").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM events WHERE type = 'TASK_RESUMED'").fetchone()[0] == 1
            assert (
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'frontier_snapshots'"
                ).fetchone()
                is None
            )
            recovered = json.loads(db.execute("SELECT checkpoint_json FROM checkpoints").fetchone()[0])
            assert recovered["budget"]["used"] == 60
            assert recovered["budget"]["by_stage"]["analysis"]["calls"] == 2
            assert recovered["state"]["stats"]["analyses_by_class"] == {"RELEVANT": 2}
    finally:
        release.set()
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
