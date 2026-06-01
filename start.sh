#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

PYTHON=python3
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON=python
fi

if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  . ".venv/bin/activate"
elif [ -f "venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  . "venv/bin/activate"
fi

if ! "$PYTHON" -c "import mcp" >/dev/null 2>&1; then
  echo "Installing dependencies from requirements.txt ..."
  if ! "$PYTHON" -m pip install -r requirements.txt; then
    echo "pip install failed. Run: $PYTHON -m pip install -r requirements.txt"
    exit 1
  fi
fi

# Default single worker (same as config).
# Multi-worker may duplicate scheduled jobs and cause SQLite lock contention.
export EDGEOPS_WORKERS=1

echo "Starting Moso at http://127.0.0.1:8010"
echo "Press Ctrl+C to stop."
echo ""

exec "$PYTHON" app.py
