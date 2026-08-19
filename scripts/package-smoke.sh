#!/usr/bin/env bash
set -euo pipefail

DRY_RUN="${DRY_RUN:-0}"
DIST_DIR="${DIST_DIR:-dist}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ "${DRY_RUN}" == "1" ]]; then
  echo "package-smoke: would build artifacts and install them in an isolated venv"
  exit 0
fi

case "${DIST_DIR}" in
  ""|"."|".."|/*|*/*)
    echo "package-smoke: DIST_DIR must be one safe relative directory name" >&2
    exit 2
    ;;
esac

rm -rf "${DIST_DIR}"
"${PYTHON_BIN}" -m build --outdir "${DIST_DIR}"
venv="$(mktemp -d)"
cleanup() {
  rm -rf "${venv}"
}
trap cleanup EXIT

"${PYTHON_BIN}" -m venv "${venv}"
mkdir -p "${venv}/empty-conductor-root"
"${venv}/bin/python" -m pip install --upgrade pip >/dev/null
"${venv}/bin/python" -m pip install "${DIST_DIR}"/*.whl >/dev/null
"${venv}/bin/sightmesh" --help >/dev/null
test "$("${venv}/bin/sightmesh" --version)" = "$("${venv}/bin/python" -c 'import importlib.metadata; print(importlib.metadata.version("sightmesh"))')"
"${venv}/bin/sightmesh" migrate --help >/dev/null
"${venv}/bin/sightmesh" --json migration-dry-run \
  --conductor-root "${venv}/empty-conductor-root" >/dev/null
"${venv}/bin/python" -c "import sightmesh, sightmesh.cli, sightmesh.conductor_migrate, sightmesh.leases; from sightmesh.runtime_lock import RUNTIME_LOCK; assert RUNTIME_LOCK.cdesktop.package.sha256"
"${PYTHON_BIN}" -m twine check "${DIST_DIR}"/*
echo "package-smoke: artifacts install and metadata validation passed"
