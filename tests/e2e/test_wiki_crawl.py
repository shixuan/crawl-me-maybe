"""A bounded live-network check; deterministic end-to-end scenarios live in smoke."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_wiki_crawl(tmp_path):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "LLM_API_KEY": "",
        "LLM_BASE_URL": "",
        "FETCH_TIMEOUT_CONNECT": "10",
        "FETCH_TIMEOUT_READ": "15",
        "FETCH_MAX_RETRIES": "1",
    }
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "from crawlme.cli import main; main()",
            "run",
            "compiler design",
            "--seeds",
            "https://en.wikipedia.org/wiki/Compiler",
            "--max-pages",
            "1",
            "--max-duration",
            "30",
            "--analysis",
            "off",
            "--dedup",
            "off",
            "--ignore-robots",
            "--result-dir",
            str(tmp_path / "results"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "COMPLETED" in result.stdout and "BUDGET_PAGES" in result.stdout, result.stdout
    dbs = list((tmp_path / "results").glob("*/db/crawl.db"))
    assert len(dbs) == 1
    with sqlite3.connect(dbs[0]) as db:
        assert db.execute("SELECT state FROM crawl_tasks").fetchone()[0] == "COMPLETED"
        pages = db.execute("SELECT title, plain_text, extraction_status FROM pages").fetchall()
        assert len(pages) == 1
        title, text, status = pages[0]
        assert "compiler" in title.lower() and "compiler" in text.lower()
        assert status == "OK"
        assert db.execute("SELECT count(*) FROM links").fetchone()[0] > 0
