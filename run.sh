#!/usr/bin/env bash
# Run RemoteCode in the foreground.
set -euo pipefail
cd "$(dirname "$0")"
exec ./venv/bin/uvicorn app:app \
  --host "${REMOTECODE_HOST:-127.0.0.1}" \
  --port "${REMOTECODE_PORT:-7070}"
