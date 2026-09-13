#!/usr/bin/env bash
# Run the CI test command in a separate environment without Playwright.
# Usage:  scripts/check-bare-lane.sh [pytest args]
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv="${BARE_VENV:-$root/.venv-bare}"
cd "$root"

if [ ! -x "$venv/bin/python" ]; then
    echo "building $venv (once, about a minute)"
    python3 -m venv "$venv"
    "$venv/bin/pip" -q install -e .
    "$venv/bin/pip" -q install pytest pytest-asyncio pytest-httpx pytest-cov
fi

if "$venv/bin/python" -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("playwright") else 1)'; then
    echo "error: $venv has playwright, so this lane is not bare" >&2
    exit 1
fi

# The command from .github/workflows/ci.yml, coverage gate included.
exec "$venv/bin/python" -m pytest -q \
    --cov=src/crawlme --cov-fail-under=70 \
    -m "not e2e and not browser" --ignore=tests/smoke "$@"
