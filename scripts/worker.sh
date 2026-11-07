#!/usr/bin/env bash
# Runs the compute worker (separate process from the web app).
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
export PYTHONPATH="$APP_DIR/src"
exec "$APP_DIR/.venv/bin/python" -m change_analysis.worker.main "$@"
