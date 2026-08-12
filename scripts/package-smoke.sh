#!/usr/bin/env bash
set -euo pipefail

DRY_RUN="${DRY_RUN:-0}"
DIST_DIR="${DIST_DIR:-dist}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ "${DRY_RUN}" == "1" ]]; then
  echo "package-smoke: would build artifacts and install them in an isolated venv"
  exit 0
fi

rm -rf "${DIST_DIR}"
"${PYTHON_BIN}" -m build
venv="$(mktemp -d)"
cleanup() {
  rm -rf "${venv}"
}
trap cleanup EXIT

"${PYTHON_BIN}" -m venv "${venv}"
"${venv}/bin/python" -m pip install --upgrade pip >/dev/null
"${venv}/bin/python" -m pip install "${DIST_DIR}"/*.whl >/dev/null
"${venv}/bin/agent-deck" --help >/dev/null
"${venv}/bin/python" -c "import agent_deck, agent_deck.cli, agent_deck.leases"
"${PYTHON_BIN}" -m twine check "${DIST_DIR}"/*
echo "package-smoke: artifacts install and metadata validation passed"
