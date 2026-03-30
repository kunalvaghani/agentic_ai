#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Create the virtual environment first: python -m venv .venv" >&2
  exit 1
fi
"$PYTHON" -m pip install -r "$PROJECT_ROOT/requirements.txt"
"$PYTHON" -m playwright install chromium
echo "Playwright Chromium installed."
