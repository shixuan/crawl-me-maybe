"""Build the labeled eval set for E4 from real benchmark crawl data.

Usage: python3 benchmark/embedding/build_eval_set.py <rule_run_dir> <local_run_dir>

Samples candidates from the two runs' candidates tables in stratified
layers and drafts relevance labels:

  off_topic_branch  football/soccer URLs                     -> false
  trap_external     junk drift domains (newsletters, mirrors,
                    social landing pages)                    -> false
  on_topic_wiki     en.wikipedia anchors matching CS vocab   -> true
  unrelated_wiki    en.wikipedia anchors on other topics     -> false
  semantic_hard     on-topic anchors with no prompt-word
                    overlap (GNU, kernel, interpreter, ...)  -> true

Sampling is deterministic (seeded Python-side random): the same run
dirs always produce the same 300 entries, so label patches stay valid.

Output: benchmark/embedding/data/embedding_eval.json.  Labels are DRAFT: apply
human corrections with apply_labels.py.
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

GOAL = "compilers, open source software and operating systems"

_TRAP_DOMAINS = (
    "web.archive.org",
    "www.facebook.com",
    "x.com",
    "twitter.com",
    "www.jolts.world",
    "firstmonday.org",
    "softwarefreedom.org",
)

_CS_ANCHOR_WORDS = (
    "compiler",
    "compiling",
    "compilation",
    "operating system",
    "operating systems",
    "open source",
    "open-source",
    "source code",
    "software",
    "linux",
    "gnu",
    "kernel",
    "unix",
    "posix",
    "programming language",
    "programming",
    "interpreter",
    "assembler",
    "assembly",
    "linker",
    "free software",
    "computer science",
    "computing",
)

_SEMANTIC_HARD_WORDS = (
    "gnu",
    "unix",
    "kernel",
    "interpreter",
    "assembler",
    "linker",
    "shell",
    "posix",
    "toolchain",
    "runtime",
    "machine code",
    "parse tree",
    "syntax",
    "computer program",
    "free software",
)


@dataclass(frozen=True)
class Layer:
    name: str
    relevant: bool
    target: int
    where: str
    params: tuple[str, ...] = field(default_factory=tuple)


_LAYERS: list[Layer] = [
    Layer(
        "off_topic_branch",
        False,
        40,
        "(url_json LIKE '%football%' OR url_json LIKE '%soccer%') AND anchor IS NOT NULL AND anchor != ''",
    ),
    Layer(
        "trap_external",
        False,
        60,
        (
            f"json_extract(url_json, '$.reg_domain') IN ({','.join('?' * len(_TRAP_DOMAINS))}) "
            "AND anchor IS NOT NULL AND anchor != ''"
        ),
        _TRAP_DOMAINS,
    ),
    Layer(
        "unrelated_wiki",
        False,
        60,
        (
            "json_extract(url_json, '$.reg_domain') = 'en.wikipedia.org' "
            "AND anchor IS NOT NULL AND anchor != '' "
            "AND LOWER(anchor) NOT LIKE '%compil%' AND LOWER(anchor) NOT LIKE '%operating%' "
            "AND LOWER(anchor) NOT LIKE '%software%' AND LOWER(anchor) NOT LIKE '%source code%' "
            "AND LOWER(anchor) NOT LIKE '%linux%' AND LOWER(anchor) NOT LIKE '%kernel%' "
            "AND LOWER(anchor) NOT LIKE '%programming%' AND LOWER(anchor) NOT LIKE '%unix%' "
            "AND LOWER(anchor) NOT LIKE '%gnu%' AND LOWER(anchor) NOT LIKE '%computer%' "
            "AND LOWER(anchor) NOT LIKE '%football%' AND LOWER(anchor) NOT LIKE '%soccer%'"
        ),
    ),
    Layer(
        "on_topic_wiki",
        True,
        100,
        (
            "json_extract(url_json, '$.reg_domain') = 'en.wikipedia.org' "
            "AND anchor IS NOT NULL AND anchor != '' "
            "AND (" + " OR ".join(f"LOWER(anchor) LIKE '%{w.replace('%', '%%')}%'" for w in _CS_ANCHOR_WORDS) + ")"
        ),
    ),
    Layer(
        "semantic_hard",
        True,
        40,
        (
            "json_extract(url_json, '$.reg_domain') = 'en.wikipedia.org' "
            "AND anchor IS NOT NULL AND anchor != '' "
            "AND (" + " OR ".join(f"LOWER(anchor) LIKE '%{w.replace('%', '%%')}%'" for w in _SEMANTIC_HARD_WORDS) + ") "
            "AND LOWER(anchor) NOT LIKE '%open source%' AND LOWER(anchor) NOT LIKE '%open-source%'"
        ),
    ),
]


def _sample(conn: sqlite3.Connection, layer: Layer, rng: random.Random) -> list[dict[str, object]]:
    rows = conn.execute(
        # trusted layer SQL: every layer.where is a constant defined above
        f"SELECT url_json, anchor, snippet, parent_heading, source_url_key, depth "  # noqa: S608
        f"FROM candidates WHERE {layer.where}",
        layer.params,
    ).fetchall()
    picked_rows = rng.sample(rows, min(layer.target, len(rows)))
    out: list[dict[str, object]] = []
    for url_json, anchor, snippet, heading, _src, depth in picked_rows:
        url = json.loads(url_json)
        parts = [anchor, snippet, heading]
        text = " ".join(p for p in parts if p).strip()
        out.append(
            {
                "text": text,
                # Kept separately: the rule scorer reads anchor, the
                # embedding scorer reads the composed text.
                "anchor": anchor or "",
                "snippet": snippet or "",
                "parent_heading": heading or "",
                "url": url.get("raw", ""),
                "domain": url.get("reg_domain", ""),
                "depth": int(depth or 0),
                "relevant": layer.relevant,
                "layer": layer.name,
                "review": layer.name in ("trap_external", "on_topic_wiki"),
            }
        )
    return out


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    rule_dir, local_dir = Path(sys.argv[1]), Path(sys.argv[2])
    # deterministic sampling, not crypto: the seed fixes the eval sample
    rng = random.Random(42)  # noqa: S311

    entries: list[dict[str, object]] = []
    seen_urls: set[str] = set()
    for layer in _LAYERS:
        picked: list[dict[str, object]] = []
        for run_dir in (local_dir, rule_dir):
            db = run_dir / "db" / "crawl.db"
            if not db.exists():
                continue
            conn = sqlite3.connect(db)
            for e in _sample(conn, layer, rng):
                e_url = str(e["url"])
                if e_url in seen_urls:
                    continue
                seen_urls.add(e_url)
                picked.append(e)
                if len(picked) >= layer.target:
                    break
            conn.close()
            if len(picked) >= layer.target:
                break
        entries.extend(picked)
        print(f"{layer.name:18} target={layer.target:3d} got={len(picked)}")

    rng.shuffle(entries)
    out = {
        "created_from": [str(rule_dir), str(local_dir)],
        "goal": {"prompt": GOAL, "candidates": entries},
    }
    out_path = Path("benchmark/embedding/data/embedding_eval.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nwrote {len(entries)} labeled candidates -> {out_path}")
    print("labels are DRAFT: apply human corrections with apply_labels.py.")


if __name__ == "__main__":
    main()
