#!/usr/bin/env bash
# Run the six acceptance checks and write runs/acceptance/.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-.venv/bin/python}"
exec "$PYTHON" -m sdd_context.verify
