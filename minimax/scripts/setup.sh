#!/usr/bin/env bash
# Create the virtualenv and install pinned dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3.12}"
"$PYTHON" -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt
echo "installed; activate with: source .venv/bin/activate"
