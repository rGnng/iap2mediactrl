#!/usr/bin/env bash
# Start the MediaCtrl web UI (needs root for USB/HID access).
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8080}" exec sudo -E .venv/bin/python -u web_app.py
