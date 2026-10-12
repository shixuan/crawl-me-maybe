"""Exercise real process interruption and CLI recovery against a local website."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from crawlme.cli.resume import cmd_resume
from crawlme.persist import Storage

# Commands use this interpreter and test-generated arguments only.
# ruff: noqa: S603


@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGKILL])
def test_cli_resume_process(tmp_path, interrupt):
    entered, release = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/second":
                entered.set()
                release.wait(20)
            body = (
                "<html><head><title>Compiler design</title></head><body><article>"
                + "<p>Compiler design involves parsing, optimization and code generation.</p>" * 20
                + ('<a href="/second">Compiler optimization techniques</a>' if self.path == "/first" else "")
                + "</article></body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {**os.environ, "LLM_API_KEY": "", "LLM_BASE_URL": "", "EXPAND_SEEDS": "false"}
    command = [sys.executable, "-c", "from crawlme.cli import main; main()"]
    root = tmp_path / "results"
    process = subprocess.Popen(
        [
            *command,
            "run",
            "compiler design",
            "--seeds",
            f"http://127.0.0.1:{server.server_port}/first",
            "--analysis",
            "off",
            "--ignore-robots",
            "--max-pages",
            "0",
            "--max-duration",
            "60",
            "--result-dir",
            str(root),
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert entered.wait(15), "crawl did not reach the blocked request"
        db_path = next(root.glob("*/db/crawl.db"))
        deadline = time.monotonic() + 5
        while True:
            with sqlite3.connect(db_path) as db:
                task_id = db.execute("SELECT task_id FROM crawl_tasks").fetchone()[0]
                payload = json.loads(db.execute("SELECT snapshot_json FROM frontier_snapshots").fetchone()[0])
            if payload["state"]["progress"]["pages_fetched"] == 1:
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        locked = subprocess.run(
            [*command, "resume", task_id, "--result-dir", str(root)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert locked.returncode == 1
        assert "already active" in locked.stdout
        assert process.poll() is None
        process.send_signal(interrupt)
        output, _ = process.communicate(timeout=10)
        assert process.returncode == (130 if interrupt == signal.SIGINT else -signal.SIGKILL), output
        release.set()
        restored = subprocess.run(
            [*command, "resume", task_id, "--result-dir", str(root)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert restored.returncode == 0, restored.stdout + restored.stderr
        assert requests.count("/first") == 1
        assert requests.count("/second") == 2
        with sqlite3.connect(db_path) as db:
            state, counters = db.execute("SELECT state, counters FROM crawl_tasks").fetchone()
            assert state == "COMPLETED"
            assert json.loads(counters)["pages_fetched"] == 2
        again = subprocess.run(
            [*command, "resume", task_id, "--result-dir", str(root)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert again.returncode == 1
        assert "only interrupted or failed" in again.stdout
    finally:
        release.set()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("payload", [None, {"ordering": {}}, {"version": 99}])
async def test_reject_old_checkpoint(tmp_path, capsys, monkeypatch, payload):
    storage = Storage.create(tmp_path)
    await storage.start()
    storage.save_task({"task_id": "test", "state": "INTERRUPTED"})
    if payload is not None:
        storage.save_snapshot({"snapshot_id": "test-latest", "snapshot_json": payload})
    await storage.close()

    def fail(*args, **kwargs):
        raise AssertionError("workers must not be created for an unsupported checkpoint")

    monkeypatch.setattr("crawlme.cli.resume.create_scheduler", fail)
    with pytest.raises(SystemExit) as error:
        await cmd_resume(argparse.Namespace(task_id="test", result_dir=str(tmp_path), log_level=None))
    assert error.value.code == 1
    assert "no supported recovery checkpoint" in capsys.readouterr().out
