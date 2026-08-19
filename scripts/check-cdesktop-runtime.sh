#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM

set -- $(PYTHONPATH="$repo_root/src" python3 -c '
from sightmesh.runtime_lock import RUNTIME_LOCK
runtime = RUNTIME_LOCK.cdesktop
print(runtime.package.url, runtime.package.sha256, runtime.version)
')
package_url=$1
package_sha256=$2
package_version=$3
curl -fsSL "$package_url" -o "$temporary/cdesktop.tgz"
PYTHONPATH="$repo_root/src" python3 -c '
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
