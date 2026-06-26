#!/usr/bin/env bash
# Launch the Logistics Copilot API (FastAPI) on port 8001.
# Run from the repo root so `backend` resolves as a package.
set -euo pipefail
cd "$(dirname "$0")"

# Activate venv if present
if [ -d "venv" ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

exec uvicorn backend.app.api:app --host 0.0.0.0 --port 8001 --reload
