#!/usr/bin/env bash
# Runs the web application.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
export PYTHONPATH="$APP_DIR/src"
exec "$APP_DIR/.venv/bin/python" main.py
