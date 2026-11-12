#!/usr/bin/env bash
# Call MiniMax live and record a new generation for S1.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${MINIMAX_API_KEY:?export MINIMAX_API_KEY first}"
PYTHON="${PYTHON:-.venv/bin/python}"
exec "$PYTHON" -m sdd_context.cli demo --sample S1_refund_cap --live --out runs/recorded/S1_refund_cap
