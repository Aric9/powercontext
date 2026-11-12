#!/usr/bin/env bash
# Run the deterministic acceptance demo (replays the recorded MiniMax output).
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-.venv/bin/python}"
exec "$PYTHON" -m sdd_context.cli demo --sample S1_refund_cap --out runs/demo/S1_refund_cap
