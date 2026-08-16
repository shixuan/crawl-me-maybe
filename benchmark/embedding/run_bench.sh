#!/usr/bin/env bash
# Benchmark: local embedding vs pure rule ranker (todo E4).
#
# Runs the SAME crawl task twice and compares:
#   1. baseline   : --embedding off  (pure RuleRanker, v0.1 behavior)
#   2. candidate  : default local embedding (MiniLM) + optional extra args
#
# Usage:
#   benchmark/run_bench.sh                                   # rule vs local
#   benchmark/run_bench.sh --embedding-model BAAI/bge-m3     # rule vs another local model
#   benchmark/run_bench.sh --embedding api \
#     --embedding-model text-embedding-3-small               # rule vs api model
#
# Each run creates a fresh timestamped results dir.  Per-run summaries
# land in <run_dir>/bench_summary.json; the comparison goes to
# results/bench_comparison.json and stdout.
set -euo pipefail

PROMPT="compilers, open source software and operating systems"
SEEDS="https://en.wikipedia.org/wiki/Compiler,https://en.wikipedia.org/wiki/Operating_system,https://en.wikipedia.org/wiki/Open-source_software,https://en.wikipedia.org/wiki/Association_football"
MAX_PAGES=300
DOMAIN_BUDGET=300

cd "$(dirname "$0")/.."

if ! command -v crawl >/dev/null 2>&1; then
    echo "error: 'crawl' not found — run: pip install -e ." >&2
    exit 1
fi

run_variant() {
    local variant="$1"
    shift
    echo
    echo "=================================================="
    echo "== variant: $variant"
    echo "== prompt : $PROMPT"
    echo "== budget : $MAX_PAGES pages"
    echo "=================================================="
    crawl run "$PROMPT" \
        --seeds "$SEEDS" \
        --max-pages "$MAX_PAGES" \
        --domain-budget "$DOMAIN_BUDGET" \
        --log-level DEBUG \
        "$@"

    local run_dir
    run_dir=$(ls -1dt results/*/ 2>/dev/null | head -1)
    if [ -z "$run_dir" ]; then
        echo "error: no results dir found under results/" >&2
        exit 1
    fi
    python3 benchmark/embedding/summarize.py "${run_dir%/}" "$variant" >/dev/null
    echo "$run_dir"
}

echo "== baseline: rule-only =="
RULE_DIR=$(run_variant "rule" --embedding off)
RULE_SUMMARY="${RULE_DIR%/}/bench_summary.json"

echo "== candidate: local embedding =="
LOCAL_DIR=$(run_variant "local" "$@")
LOCAL_SUMMARY="${LOCAL_DIR%/}/bench_summary.json"

echo
echo "== comparison: rule vs local =="
python3 - "$RULE_SUMMARY" "$LOCAL_SUMMARY" <<'PY'
import json
import sys
from pathlib import Path

rule = json.loads(Path(sys.argv[1]).read_text())
cand = json.loads(Path(sys.argv[2]).read_text())

def fmt(v):
    return f"{v:.1%}" if isinstance(v, float) and v <= 1.0 else str(v)

rows = [
    ("pages fetched", rule["pages_fetched"], cand["pages_fetched"]),
    ("candidates discovered", rule["candidates_discovered"], cand["candidates_discovered"]),
    ("rank decisions", rule["rank_decisions"], cand["rank_decisions"]),
    ("en.wikipedia share", rule["en_wikipedia_share"], cand["en_wikipedia_share"]),
    ("off-topic branch pages", rule["off_topic_branch_pages"], cand["off_topic_branch_pages"]),
    ("embedding cache rows", rule["embedding_cache_rows"], cand["embedding_cache_rows"]),
]
if cand["embedding_sim_percentiles"]:
    p = cand["embedding_sim_percentiles"]
    rows.append(("emb sim p50 (candidate only)", "-", p["p50"]))
    rows.append(("emb sim p90 (candidate only)", "-", p["p90"]))

print(f"{'metric':28} {'rule-only':>16} {'local-embedding':>16}")
print("-" * 64)
for name, r, c in rows:
    print(f"{name:28} {fmt(r):>16} {fmt(c):>16}")

comparison = {
    "rule_only": {"variant": rule["variant"], "run_dir": rule["run_dir"], "summary": rule},
    "candidate": {"variant": cand["variant"], "run_dir": cand["run_dir"], "summary": cand},
}
out = Path("results") / "bench_comparison.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(comparison, indent=2))
print(f"\ncomparison written: {out}")
PY
