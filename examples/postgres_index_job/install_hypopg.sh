#!/usr/bin/env bash
set -euo pipefail

# Ensure the hypopg extension exists on the target database.
# Requires:
#   - PG_CONN_STR env var (postgresql://user@host:port/dbname)
#   - psql on PATH

if [ -z "${PG_CONN_STR:-}" ]; then
  echo "PG_CONN_STR is not set. Export it and rerun. Example:"
  echo "  export PG_CONN_STR=\"postgresql://user@localhost:5432/postgres\""
  exit 1
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "psql not found on PATH. Install Postgres client tools (e.g., brew install libpq) and retry."
  exit 1
fi

echo "Creating hypopg extension on ${PG_CONN_STR} ..."
psql "${PG_CONN_STR}" -c "CREATE EXTENSION IF NOT EXISTS hypopg;" && echo "hypopg ready."

