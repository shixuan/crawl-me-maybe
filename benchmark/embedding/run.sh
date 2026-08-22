#!/usr/bin/env bash
# Does the embedding stage earn its place?  One command, two answers.
#
#   ./benchmark/embedding/run.sh
#   ./benchmark/embedding/run.sh --session ./ig-session.json --seeds ./accounts.json
#   ./benchmark/embedding/run.sh --run results/20260822_101010   # score only
#
# A walled platform needs --session.  It is never stored here: pass the
# path, or set SESSION, and the crawl reads it.
#
# The crawl must be --recall, or the scoring measures survivors and every
# ranker looks equally good on those.  See README.md.
set -euo pipefail

PROMPT=${PROMPT:-"software releases and version announcements, with the project name, the version number and what changed"}
SEEDS=${SEEDS:-"https://blog.rust-lang.org/feed.xml,https://github.blog/feed/,https://pythoninsider.blogspot.com/feeds/posts/default,https://news.ycombinator.com/rss"}
PAGES=${PAGES:-120}
RESULTS=${RESULTS:-results}
SESSION=${SESSION:-}
RUN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run) RUN="$2"; shift 2 ;;
    --pages) PAGES="$2"; shift 2 ;;
    --prompt) PROMPT="$2"; shift 2 ;;
    --seeds) SEEDS="$2"; shift 2 ;;
    --session) SESSION="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
cd "$root"

if [[ -z "$RUN" ]]; then
  command -v crawl >/dev/null || { echo "crawl is not on PATH: pip install -e ." >&2; exit 1; }
  # Run directories only: the results tree also holds the cache
  # databases, and those sort after every timestamp, so "the last thing
  # listed" was always feedback.db and never changed.
  newest() { find "$RESULTS" -maxdepth 1 -type d -name '2*' -printf '%f\n' 2>/dev/null | sort | tail -1; }
  before=$(newest)

  echo "== crawling with --recall (nothing is dropped, so everything gets a verdict)"
  echo "   prompt : $PROMPT"
  echo "   seeds  : $SEEDS"
  echo "   budget : $PAGES pages of LLM spend"
  args=(run "$PROMPT" --seeds "$SEEDS" --recall --page-budget "$PAGES")
  if [[ -n "$SESSION" ]]; then
    [[ -f "$SESSION" ]] || { echo "no session file at $SESSION" >&2; exit 1; }
    echo "   session: $SESSION"
    args+=(--session "$SESSION")
  fi
  echo
  crawl "${args[@]}"

  after=$(newest)
  [[ "$after" != "$before" ]] || { echo "no new run directory appeared under $RESULTS" >&2; exit 1; }
  RUN="$RESULTS/$after"
  echo
  echo "== run: $RUN"
fi

[[ -f "$RUN/db/crawl.db" ]] || { echo "no database at $RUN/db/crawl.db" >&2; exit 1; }

# Both settings over the same candidates, so the only thing that differs
# is how much of each one the model was allowed to read.
echo
echo "======== as shipped ========"
python3 "$here/score.py" "$RUN"
echo
echo "======== at the model's real ceiling ========"
python3 "$here/score.py" "$RUN" --max-tokens 512
echo
echo "== scored: $RUN"
