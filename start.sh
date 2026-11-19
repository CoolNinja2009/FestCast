#!/usr/bin/env bash
# Fedora / Linux launcher
set -e
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { python3 -m venv .venv; .venv/bin/pip install -r requirements.txt; }
[ -f frontend/dist/index.html ] || (cd frontend && npm install --no-fund --no-audit && npm run build)
[ -f .env ] || cp .env.example .env
exec .venv/bin/python -m backend.main --open
