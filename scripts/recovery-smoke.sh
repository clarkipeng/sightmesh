#!/usr/bin/env bash
set -euo pipefail

PORT="${SMOKE_PORT:-43210}"
DRY_RUN="${DRY_RUN:-1}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "recovery-smoke: dry_run=${DRY_RUN} port=${PORT}"
echo "recovery-smoke: refusing to touch Conductor workers or unmanaged labels"

if [[ "${DRY_RUN}" == "1" ]]; then
  echo "recovery-smoke: would verify SightMesh service and lease recovery in a temporary HOME"
  exit 0
fi

tmpdir="$(mktemp -d)"
cleanup() {
  rm -rf "${tmpdir}"
}
trap cleanup EXIT

mkdir -p "${tmpdir}/bin"
printf '#!/usr/bin/env bash\nsleep 1\n' >"${tmpdir}/bin/cdesktop"
printf '#!/usr/bin/env bash\nexit 0\n' >"${tmpdir}/bin/sightmesh"
chmod +x "${tmpdir}/bin/cdesktop" "${tmpdir}/bin/sightmesh"
mkdir -p "${tmpdir}/repo" "${tmpdir}/worktree"

HOME="${tmpdir}" PATH="${tmpdir}/bin:${PATH}" PYTHONPATH="${PWD}/src" "${PYTHON_BIN}" - <<'PY'
import json
from pathlib import Path

from sightmesh import service
from sightmesh.leases import LeaseStore

plist = service.install(port=43210, start_now=False)
bridge_plist = service.bridge_plist_path()
assert plist.exists(), plist
assert bridge_plist.exists(), bridge_plist
assert str(plist).startswith(str(Path.home()))
assert str(bridge_plist).startswith(str(Path.home()))

store = LeaseStore()
repo = Path.home() / "repo"
worktree = Path.home() / "worktree"
lease = store.acquire("smoke", repo, worktree, ttl_seconds=60, workspace_id="smoke-ws")
assert store.workspace_token("smoke-ws") == lease.token
released = store.release_workspace("smoke-ws")
assert released.token == lease.token
print(json.dumps({"service_plists": 2, "lease_release": True}, sort_keys=True))
PY

echo "recovery-smoke: temporary HOME service and lease recovery verified"
