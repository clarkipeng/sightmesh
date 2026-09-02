#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
manifest_dir="$HOME/.local/state/sightmesh"
manifest="$manifest_dir/install-manifest.json"
owned_paths=""

record_path() {
  owned_paths="$owned_paths$1
"
}

# Install owns only what it creates. Refusing to displace a foreign path is
# what makes uninstall a pure delete of owned entries, with nothing to
# restore and nothing silently taken from another product.
link_skill() {
  skill_name=$1
  source_path="$repo_root/skills/$skill_name"
  for skill_root in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
    mkdir -p "$skill_root"
    destination="$skill_root/$skill_name"
    if [ -L "$destination" ] && [ "$(readlink "$destination")" = "$source_path" ]; then
      record_path "$destination"
      continue
    fi
    if [ -e "$destination" ] || [ -L "$destination" ]; then
      echo "Refusing to replace a skill path SightMesh does not own: $destination" >&2
      echo "Remove it with its own product's uninstaller first." >&2
      exit 1
    fi
    ln -s "$source_path" "$destination"
    record_path "$destination"
  done
}

write_manifest() {
  mkdir -p "$manifest_dir"
  chmod 700 "$manifest_dir"
  tool_dir=$(uv tool dir 2>/dev/null || true)
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "repo_root": "%s",\n' "$repo_root"
    printf '  "uv_tool": "%s",\n' "${tool_dir:+$tool_dir/sightmesh}"
    printf '  "created_paths": [\n'
    printf '%s' "$owned_paths" | awk 'NF' | awk '{ printf "%s    \"%s\"", (NR>1 ? ",\n" : ""), $0 } END { printf "\n" }'
    printf '  ],\n'
    printf '  "state_paths": [\n'
    printf '    "%s",\n' "$HOME/.local/share/sightmesh"
    printf '    "%s",\n' "$HOME/.local/state/sightmesh"
    printf '    "%s",\n' "$HOME/Library/LaunchAgents/io.sightmesh.cdesktop.plist"
    printf '    "%s",\n' "$HOME/Library/LaunchAgents/io.sightmesh.bridge.plist"
    printf '    "%s"\n' "$HOME/Library/LaunchAgents/io.sightmesh.updater.plist"
    printf '  ]\n'
    printf '}\n'
  } >"$manifest"
  chmod 600 "$manifest"
}

uv tool install --editable "$repo_root" --force

link_skill orchestrate-visible-agents
link_skill reconcile-agent-work

write_manifest

echo "Installed SightMesh and linked shared Claude/Codex skills."
echo "Owned paths recorded in $manifest; scripts/uninstall-local.sh reverses them."
