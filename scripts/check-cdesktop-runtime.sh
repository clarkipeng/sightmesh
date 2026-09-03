#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
temporary=$(mktemp -d)
cleanup() { status=$?; rm -rf "$temporary"; exit "$status"; }
trap cleanup EXIT HUP INT TERM

# A plain assignment fails the script when the import fails; `set -- $(...)`
# would swallow the error because `set` itself succeeds.
lock_fields=$(python3 -c '
from sightmesh.runtime_lock import RUNTIME_LOCK
runtime = RUNTIME_LOCK.cdesktop
print(runtime.package.url, runtime.package.sha256, runtime.version)
')
read -r package_url package_sha256 package_version <<<"$lock_fields"
[ -n "$package_version" ] || { echo "runtime lock did not yield url, sha256 and version" >&2; exit 1; }
curl -fsSL "$package_url" -o "$temporary/cdesktop.tgz"
python3 -c '
import sys
from sightmesh.runtime_lock import verify_file_sha256
verify_file_sha256(sys.argv[1], sys.argv[2])
' "$temporary/cdesktop.tgz" "$package_sha256"
npm install --prefix "$temporary/install" --ignore-scripts --no-audit --no-fund \
  "$temporary/cdesktop.tgz" >/dev/null
reported=$("$temporary/install/node_modules/.bin/cdesktop" --version)
case "$reported" in
  "cdesktop/$package_version "*) ;;
  *) echo "Pinned cdesktop reported an unexpected version: $reported" >&2; exit 1 ;;
esac
echo "runtime-compatibility: verified $package_version package checksum and executable"
