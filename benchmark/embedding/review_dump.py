"""Dump a compact review listing of the eval set for human spot-checks.

One line per candidate: index, layer, current label, anchor text, domain,
plus the fetched page title/excerpt when available.

Usage: python3 benchmark/embedding/review_dump.py > benchmark/embedding/data/review_list.txt
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path("benchmark/embedding/data/embedding_eval.json")


def main() -> None:
    data = json.loads(EVAL.read_text())
    entries = data["goal"]["candidates"]
    out: list[str] = []
    for i, e in enumerate(entries):
        anchor = (e.get("anchor") or e.get("text") or "")[:85].replace("\n", " ")
        extra = ""
        if e.get("page_title"):
            extra = f"  || PAGE: {e['page_title'][:60]} | {(e.get('page_excerpt') or '')[:90]}"
        out.append(f"{i:03d} [{e['layer'][:14]:14}] rel={int(e['relevant'])} {anchor}  ({e['domain']}){extra}")
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
