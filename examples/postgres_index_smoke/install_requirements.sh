#!/usr/bin/env bash
set -euo pipefail

# Install openevolve (editable) and example-specific requirements into the local .venv.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
VENV_PATH="${SCRIPT_DIR}/.venv"
REQ_FILE="${SCRIPT_DIR}/requirements.txt"

if [ ! -d "${VENV_PATH}" ]; then
  echo ".venv not found. Creating via install_env.sh..."
  bash "${SCRIPT_DIR}/install_env.sh"
fi

PY_BIN="${VENV_PATH}/bin/python"

echo "Installing openevolve (editable) from ${REPO_ROOT}..."
uv pip install --python "${PY_BIN}" -e "${REPO_ROOT}"

echo "Installing example requirements from ${REQ_FILE}..."
uv pip install --python "${PY_BIN}" -r "${REQ_FILE}"

echo "Done. Interpreter: ${PY_BIN}"

