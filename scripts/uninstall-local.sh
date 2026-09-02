#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

check_link() {
  skill_name=$1
  source_path="$repo_root/skills/$skill_name"
  for skill_root in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
    destination="$skill_root/$skill_name"
    if [ -L "$destination" ]; then
      if [ "$(readlink "$destination")" = "$source_path" ]; then
        :
      else
        echo "Refusing to remove unrelated skill link: $destination" >&2
        exit 1
      fi
    elif [ -e "$destination" ]; then
      echo "Refusing to remove non-link skill path: $destination" >&2
      exit 1
    fi
  done
}

remove_link() {
  skill_name=$1
  for skill_root in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
    destination="$skill_root/$skill_name"
    if [ -L "$destination" ]; then
      rm "$destination"
    fi
  done
}

check_link orchestrate-visible-agents
check_link reconcile-agent-work

for label in io.sightmesh.cdesktop io.sightmesh.bridge io.sightmesh.updater; do
  plist="$HOME/Library/LaunchAgents/$label.plist"
  if [ -L "$plist" ]; then
    echo "Refusing to remove symlinked LaunchAgent: $plist" >&2
    exit 1
  fi
  if [ -e "$plist" ]; then
    installed_label=$(plutil -extract Label raw "$plist" 2>/dev/null || true)
    if [ "$installed_label" != "$label" ]; then
      echo "Refusing to remove unrelated LaunchAgent: $plist" >&2
      exit 1
    fi
  fi
done

remove_link orchestrate-visible-agents
remove_link reconcile-agent-work

remove_owned_tool() {
  tool_dir=$(uv tool dir 2>/dev/null || true)
  metadata=$(find "$tool_dir/sightmesh" -type f -path '*/site-packages/sightmesh-*.dist-info/direct_url.json' -print -quit 2>/dev/null || true)
  if [ -n "$metadata" ] && grep -F '"url":"file://'$repo_root'"' "$metadata" >/dev/null 2>&1; then
    uv tool uninstall sightmesh
  else
    echo "Left uv tool sightmesh installed: ownership could not be verified."
  fi
}

remove_owned_tool

for label in io.sightmesh.cdesktop io.sightmesh.bridge io.sightmesh.updater; do
  plist="$HOME/Library/LaunchAgents/$label.plist"
  if [ -e "$plist" ]; then
    launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
    rm "$plist"
  fi
done

# The manifest lists only paths install created, so removing it last leaves
# no record of an installation that no longer exists.
rm -f "$HOME/.local/state/sightmesh/install-manifest.json"

echo "Removed this SightMesh installation and its owned local links."
echo "Durable state under ~/.local/share/sightmesh and ~/.local/state/sightmesh is kept; delete it explicitly to finish."
