#!/usr/bin/env bash
# Starts the catalogue web interface.
#   ./run.sh              -> http://127.0.0.1:8000
#   ./run.sh 0.0.0.0      -> listen on the network (read the security section first)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f data/catalogue.db ]; then
  echo "No database yet. Run:  .venv/bin/python etl/build_db.py" >&2
  exit 1
fi

HOST="${1:-127.0.0.1}"
PORT="${2:-8000}"
echo "Catalogue  ->  http://${HOST}:${PORT}"
exec .venv/bin/uvicorn web.app:app --host "$HOST" --port "$PORT"
