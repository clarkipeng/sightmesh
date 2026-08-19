#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

set -- $(PYTHONPATH="$repo_root/src" python3 -c '
from sightmesh.runtime_lock import RUNTIME_LOCK
runtime = RUNTIME_LOCK.cdesktop
print(runtime.package.url, runtime.package.sha256)
')
pinned_package=$1
pinned_sha256=$2
package=${CDESKTOP_PACKAGE:-$pinned_package}
expected_sha256=${CDESKTOP_SHA256:-}
local_development=${SIGHTMESH_LOCAL_DEVELOPMENT:-0}
if [ "$package" = "$pinned_package" ] && [ -z "$expected_sha256" ]; then
    expected_sha256=$pinned_sha256
elif [ -z "$expected_sha256" ] && [ "$local_development" != "1" ]; then
    echo "CDESKTOP_PACKAGE overrides require CDESKTOP_SHA256 or SIGHTMESH_LOCAL_DEVELOPMENT=1" >&2
    exit 1
fi

package_archive=$(mktemp "${TMPDIR:-/tmp}/sightmesh-cdesktop.XXXXXX.tgz")
trap 'rm -f "$package_archive"' EXIT HUP INT TERM
case "$package" in
    https://*) curl -fsSL "$package" -o "$package_archive" ;;
    *) cp "$package" "$package_archive" ;;
esac
if [ -n "$expected_sha256" ]; then
    PYTHONPATH="$repo_root/src" python3 -c '
import sys
from sightmesh.runtime_lock import verify_file_sha256
verify_file_sha256(sys.argv[1], sys.argv[2])
' "$package_archive" "$expected_sha256"
fi
npm install -g "$package_archive"
uv tool install "git+https://github.com/prassanna-ravishankar/repowire.git@v0.17.0" --force
uv pip install --python "$HOME/.local/share/uv/tools/repowire/bin/python" "mcp<2"
repowire setup --non-interactive --no-update-checks

"$repo_root/scripts/install-local.sh"
sightmesh service install --no-start

echo "Bootstrap complete. Review 'sightmesh doctor' before starting the service."
