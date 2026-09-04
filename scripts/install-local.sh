#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
manifest_dir="$HOME/.local/state/sightmesh"
manifest="$manifest_dir/install-manifest.json"
skill_names="orchestrate-visible-agents reconcile-agent-work"
skill_roots="$HOME/.claude/skills $HOME/.codex/skills"
owned_paths=""

record_path() {
  owned_paths="$owned_paths$1
"
}

# Install owns only what it creates. Refusing to displace a foreign path is
# what makes uninstall a pure delete of owned entries, with nothing to
# restore and nothing silently taken from another product.
check_skill() {
  skill_name=$1
  source_path="$repo_root/skills/$skill_name"
  for skill_root in $skill_roots; do
    destination="$skill_root/$skill_name"
    if [ -L "$destination" ] && [ "$(readlink "$destination")" = "$source_path" ]; then
      continue
    fi
    if [ -e "$destination" ] || [ -L "$destination" ]; then
      echo "Refusing to replace a skill path SightMesh does not own: $destination" >&2
      echo "Remove it with its own product's uninstaller first." >&2
      exit 1
    fi
  done
}

link_skill() {
  skill_name=$1
  source_path="$repo_root/skills/$skill_name"
  for skill_root in $skill_roots; do
    mkdir -p "$skill_root"
    destination="$skill_root/$skill_name"
    if [ ! -L "$destination" ]; then
      ln -s "$source_path" "$destination"
    fi
    record_path "$destination"
  done
}

# Written to a temporary file and moved into place: a manifest that is the
# record of what to remove must never be observed half-written.
write_manifest() {
  mkdir -p "$manifest_dir"
  chmod 700 "$manifest_dir"
  tool_dir=$(uv tool dir 2>/dev/null || true)
  temporary="$manifest.$$.tmp"
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
  } >"$temporary"
  chmod 600 "$temporary"
  mv "$temporary" "$manifest"
}

# Validate everything, then act. A refusal must leave the host exactly as it
# was found; installing the tool first and only then discovering a skill
# path we do not own left a half-installed machine behind.
for skill in $skill_names; do
  check_skill "$skill"
done

uv tool install --editable "$repo_root" --force

for skill in $skill_names; do
  link_skill "$skill"
done

write_manifest

echo "Installed SightMesh and linked shared Claude/Codex skills."
echo "Owned paths recorded in $manifest; scripts/uninstall-local.sh removes exactly those."
