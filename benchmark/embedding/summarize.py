"""Extract benchmark stats from a crawl results directory.

Usage: python3 bench/summarize.py <results_dir> <variant_name>

Prints a JSON summary and writes it to <results_dir>/bench_summary.json.
Variant name tags which pipeline produced the run (rule / local / api).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def summarize(run_dir: Path, variant: str) -> dict[str, object]:
    db_path = run_dir / "db" / "crawl.db"
    if not db_path.exists():
        raise SystemExit(f"error: {db_path} not found")

    conn = sqlite3.connect(db_path)
    pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    candidates = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    rank_total = conn.execute("SELECT COUNT(*) FROM rank_decisions").fetchone()[0]
    by_ranker = dict(conn.execute("SELECT ranker, COUNT(*) FROM rank_decisions GROUP BY ranker").fetchall())

    sims = [r[0] for r in conn.execute("SELECT priority FROM rank_decisions WHERE ranker='embedding'").fetchall()]
    sim_percentiles: dict[str, float] = {}
    if sims:
        sims.sort()
        sim_percentiles = {
            "p10": round(sims[len(sims) // 10], 4),
            "p50": round(sims[len(sims) // 2], 4),
            "p90": round(sims[(len(sims) * 9) // 10], 4),
            "min": round(sims[0], 4),
            "max": round(sims[-1], 4),
        }

    top_domains = [
        [d, n]
        for d, n in conn.execute(
            "SELECT json_extract(url_json, '$.domain'), COUNT(*) FROM pages GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
        ).fetchall()
    ]
    en_share = 0.0
    for d, n in top_domains:
        if d == "en.wikipedia.org":
            en_share = round(n / pages, 4) if pages else 0.0
    off_topic = conn.execute(
        "SELECT COUNT(*) FROM pages WHERE url_json LIKE '%football%' OR url_json LIKE '%soccer%'"
    ).fetchone()[0]
    conn.close()

    cache_rows = 0
    cache_path = run_dir.parent / "embedding_cache.db"
    if cache_path.exists():
        c2 = sqlite3.connect(cache_path)
        cache_rows = c2.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        c2.close()

    return {
        "variant": variant,
        "run_dir": str(run_dir),
        "pages_fetched": pages,
        "candidates_discovered": candidates,
        "rank_decisions": rank_total,
        "rank_decisions_by_ranker": by_ranker,
        "embedding_sim_percentiles": sim_percentiles,
        "en_wikipedia_share": en_share,
        "top_domains_by_pages": top_domains,
        "off_topic_branch_pages": off_topic,
        "embedding_cache_rows": cache_rows,
    }


def main() -> None:
    run_dir = Path(sys.argv[1].rstrip("/"))
    variant = sys.argv[2] if len(sys.argv) > 2 else "unknown"
    summary = summarize(run_dir, variant)
    (run_dir / "bench_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
