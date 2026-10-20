"""Exercise the real CLI and provider HTTP transport through persisted dashboard results."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from dashboard.serve import Handler as DashboardHandler


@pytest.fixture(scope="module")
def completed_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("full-run")
    requests, calls, failures = [], [], []
    pages = {
        "/": (
            "Workshop directory",
            "Browse the workshop announcements.",
            ["/a", "/b", "/expired", "/old", "/unrelated", "/notes.pdf"],
        ),
        "/extra": ("Another directory", "Browse more workshop announcements.", ["/a"]),
        "/a": ("Open workshop", "Free workshop. Registration ends December 31, 2099.", []),
        "/b": ("Workshop announcement", "Free workshop. Registration ends December 31, 2099.", []),
        "/expired": ("Expired workshop", "Past workshop. Registration ends January 1, 2001.", []),
        "/old": ("Old publication", "An old announcement of a free workshop.", []),
        "/unrelated": ("Store catalog", "Ordinary products for sale, with no workshops.", []),
    }

    class Provider(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path not in pages:
                self.send_error(404)
                return
            title, body, links = pages[self.path]
            published = "2000-01-01" if self.path == "/old" else "2026-10-01"
            html = (
                f"<html><head><title>{title}</title>"
                f'<meta property="article:published_time" content="{published}"></head>'
                f"<body><article><h1>{title}</h1>"
                + f"<p>{body}</p>" * 10
                + "".join(f'<a href="{link}">{pages.get(link, ("Download notes",))[0]}</a>' for link in links)
                + "</article></body></html>"
            )
            self.send_body(html.encode(), "text/html")

        def do_POST(self):
            try:
                assert self.path == "/v1/chat/completions"
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                prompt = data["messages"][-1]["content"]
                system = data["messages"][0]["content"]
                if "## Seeds already chosen" in prompt:
                    calls.append("seeds")
                    answer = {"seeds": [{"url": site + "/extra", "why": "More workshops"}]}
                elif "## Candidate links" in prompt:
                    calls.append("ranking")
                    answer = {
                        "rankings": [{"id": name, "priority": 0.9} for name in re.findall(r"^(c\d+):", prompt, re.M)],
                        "candidates_to_drop": [],
                    }
                elif "## Page\n" in prompt:
                    path = urlsplit(prompt.split("## Page\n", 1)[1].splitlines()[0]).path
                    calls.append(path)
                    assert path in pages
                    if path in {"/a", "/b", "/expired"}:
                        title, body, _ = pages[path]
                        date = "January 1, 2001" if path == "/expired" else "December 31, 2099"
                        answer = {
                            "classification": "RELEVANT",
                            "relevance_score": 0.9,
                            "items": [
                                {
                                    "summary": title,
                                    "relevance_score": 0.9,
                                    "evidence": [body],
                                    "extracted": {"offer": {"value": body.split(".")[0], "evidence": body}},
                                    "time": {"ends_on": {"value": date, "evidence": [body]}},
                                }
                            ],
                        }
                    else:
                        answer = {
                            "classification": "IRRELEVANT",
                            "relevance_score": 0,
                            "summary": "No workshop is offered on this page.",
                            "items": [],
                        }
                elif prompt.startswith("{"):
                    calls.append("dedup")
                    rows = json.loads(prompt)["results"]
                    duplicates = [r["item_id"] for r in rows if urlsplit(r["url"]).path in {"/a", "/b"}]
                    assert len(duplicates) == 2
                    answer = {"groups": [{"members": duplicates, "overview": "Open workshop"}]}
                else:
                    assert "goal_statement" in system
                    calls.append("goal")
                    answer = {
                        "goal_statement": "Find free workshops",
                        "keywords": ["workshop"],
                        "time_policy": "Registration deadline",
                        "extraction_spec": {"fields": {"offer": "The workshop offered"}},
                    }
                response = {
                    "id": "local",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "gpt-4o-mini",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": json.dumps(answer)},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
                self.send_body(json.dumps(response).encode(), "application/json")
            except Exception as exc:
                failures.append(repr(exc))
                self.send_error(500)

        def send_body(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    site = f"http://127.0.0.1:{server.server_port}"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(root),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "LLM_API_KEY": "local-test",
        "LLM_BASE_URL": site + "/v1",
        "LLM_MODEL": "openai/gpt-4o-mini",
        "LLM_DEDUP_REASONING_EFFORT": "",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "FETCH_CONCURRENCY": "2",
        "LLM_CONCURRENCY": "2",
        "NO_PROXY": "127.0.0.1",
        "EXPAND_SEEDS_MIN": "1",
        "EXPAND_SEEDS_MAX": "1",
    }
    try:
        proc = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-c",
                "from crawlme.cli import main; main()",
                "run",
                "Find free workshops",
                "--seeds",
                site + "/",
                "--expand-seeds",
                "--since",
                "2026-01-01",
                "--draining",
                "--ignore-robots",
                "--max-duration",
                "30",
                "--max-tokens",
                "5000",
                "--result-dir",
                str(root / "results"),
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert not failures, failures
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "COMPLETED (FRONTIER_DRAINED)" in proc.stdout, proc.stdout
        assert "dedup" in proc.stdout
        assert {"goal", "seeds", "ranking", "dedup", "/a", "/b", "/expired", "/unrelated", "/extra"} <= set(calls), (
            calls
        )
        assert calls.count("dedup") == 1
        assert "/old" in requests and "/old" not in calls
        assert "/notes.pdf" not in requests
        assert requests.count("/extra") >= 1
        dbs = list((root / "results").glob("*/db/crawl.db"))
        assert len(dbs) == 1
        with sqlite3.connect(dbs[0]) as db:
            task, state = db.execute("SELECT task_id, state FROM crawl_tasks").fetchone()
            assert state == "COMPLETED"
            assert db.execute("SELECT count(*) FROM dedup_runs").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM analyses WHERE classification='IRRELEVANT'").fetchone()[0] >= 1
        inspect_env = {**env, "RESULT_DIR": str(root / "results")}
        exported = subprocess.run(  # noqa: S603
            [sys.executable, "-c", "from crawlme.cli import main; main()", "inspect", task, "--export", "json"],
            cwd=root,
            env=inspect_env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert exported.returncode == 0, exported.stderr
        assert any(r["classification"] == "IRRELEVANT" for r in json.loads(exported.stdout))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    class Dashboard(DashboardHandler):
        results_dir = root / "results"

    dashboard = ThreadingHTTPServer(("127.0.0.1", 0), Dashboard)
    thread = threading.Thread(target=dashboard.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{dashboard.server_port}", dbs[0].parent.parent.name
    finally:
        dashboard.shutdown()
        dashboard.server_close()
        thread.join(timeout=5)


def test_full_run(completed_run):
    url, run = completed_run
    with httpx.Client(trust_env=False) as client:
        response = client.get(f"{url}/api/run/{run}")
        response.raise_for_status()
        data = response.json()
    rows = {urlsplit(row["url"]).path: row for row in data["rows"]}
    assert rows["/a"]["extracted"]["offer"]["value"] == "Free workshop"
    assert rows["/unrelated"]["classification"] == "IRRELEVANT"
    assert rows["/unrelated"]["summary"]
    assert rows["/expired"]["when"] == "over"
    assert rows["/a"]["when"] == rows["/b"]["when"] == "open"
    assert "/old" not in rows
    assert len(data["groups"]) == 2
    assert any(set(g["members"]) == {rows["/a"]["item_id"], rows["/b"]["item_id"]} for g in data["groups"])


@pytest.mark.browser
def test_dashboard_results(completed_run):
    pytest.importorskip("playwright")
    from playwright.sync_api import expect, sync_playwright

    url, _ = completed_run
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route(
                "**/*", lambda route: route.continue_() if route.request.url.startswith(url + "/") else route.abort()
            )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url)
            expect(page.locator("#cards > .result")).to_have_count(1)
            expect(page.locator("#cards")).to_contain_text("Open workshop")
            page.locator("#when-chips button", has_text="past").click()
            expect(page.locator("#cards > .result")).to_have_count(2)
            expect(page.locator("#cards")).to_contain_text("Expired workshop")
            page.locator("#chips button", has_text="irrelevant").click()
            expect(page.locator("#cards")).to_contain_text("No workshop is offered on this page.")
            assert not errors
        finally:
            browser.close()
