#!/usr/bin/env bash
# Render start script — ensure repo root is on PYTHONPATH before uvicorn loads backend.*
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-.}"
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
