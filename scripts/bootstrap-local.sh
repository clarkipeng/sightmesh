#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

npm install -g "https://github.com/clarkipeng/cdesktop/releases/download/v0.2.5-20260813115508/cdesktop-0.2.5.tgz"
uv tool install "git+https://github.com/prassanna-ravishankar/repowire.git@v0.17.0" --force
uv pip install --python "$HOME/.local/share/uv/tools/repowire/bin/python" "mcp<2"
repowire setup --non-interactive --no-update-checks

"$repo_root/scripts/install-local.sh"
sightmesh service install --no-start

echo "Bootstrap complete. Review 'sightmesh doctor' before starting the service."
