#!/usr/bin/env bash
set -euo pipefail

# Local setup orchestration for the Postgres/HypoPG smoke test.
# Runs modular sub-scripts:
#   1) install_env.sh           -> create/reuse .venv under this folder
#   2) install_requirements.sh  -> install openevolve editable + example deps
#   3) install_hypopg.sh        -> (optional) create hypopg extension in target DB

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "Step 1/3: create/reuse .venv"
bash "${SCRIPT_DIR}/install_env.sh"

echo "Step 2/3: install openevolve + example requirements"
bash "${SCRIPT_DIR}/install_requirements.sh"

echo "Step 3/3: ensure hypopg extension (optional)"
if [ -n "${PG_CONN_STR:-}" ]; then
  bash "${SCRIPT_DIR}/install_hypopg.sh"
else
  echo "PG_CONN_STR not set; skipping hypopg. Set it and rerun this script to install."
fi

echo "Setup complete. To run:"
echo "  export PG_CONN_STR=\"postgresql://user@localhost:5432/postgres\""
echo "  export OPENAI_API_KEY=...  # any OpenAI-compatible key"
echo "  ${SCRIPT_DIR}/.venv/bin/python ${SCRIPT_DIR}/run_job.py"

