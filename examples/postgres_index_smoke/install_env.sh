#!/usr/bin/env bash
set -euo pipefail

# Create/reuse a uv-managed virtualenv local to this example.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PATH="${SCRIPT_DIR}/.venv"

echo "Example venv: ${VENV_PATH}"

if [ ! -d "${VENV_PATH}" ]; then
  echo "Creating venv with uv..."
  uv venv "${VENV_PATH}"
else
  echo "Using existing venv."
fi

echo "Activate with: source ${VENV_PATH}/bin/activate"

