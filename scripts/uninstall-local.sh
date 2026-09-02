#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
manifest="$HOME/.local/state/sightmesh/install-manifest.json"
skill_names="orchestrate-visible-agents reconcile-agent-work"
skill_roots="$HOME/.claude/skills $HOME/.codex/skills"

# The manifest is the record of what this installation created, so it is
# what uninstall reads. An installation that predates the manifest, or whose
# manifest was removed by hand, falls back to the paths install has always
# created.
manifest_created_paths() {
  [ -f "$manifest" ] || return 0
  sed -n '/"created_paths": \[/,/^  \]/p' "$manifest" |
    sed -n 's/^ *"\(.*\)",\{0,1\}$/\1/p'
}

owned_skill_paths() {
  recorded=$(manifest_created_paths)
  if [ -n "$recorded" ]; then
    printf '%s\n' "$recorded"
    return 0
  fi
  for skill_name in $skill_names; do
    for skill_root in $skill_roots; do
      printf '%s\n' "$skill_root/$skill_name"
    done
  done
}

check_owned_link() {
  destination=$1
  if [ -L "$destination" ]; then
    case "$(readlink "$destination")" in
      "$repo_root"/skills/*) return 0 ;;
      *)
        echo "Refusing to remove unrelated skill link: $destination" >&2
        exit 1
        ;;
    esac
  elif [ -e "$destination" ]; then
    echo "Refusing to remove non-link skill path: $destination" >&2
    exit 1
  fi
}

owned=$(owned_skill_paths)

# Validate every recorded path before removing any of it.
if ! printf '%s\n' "$owned" | while IFS= read -r destination; do
  [ -n "$destination" ] || continue
  check_owned_link "$destination"
done; then
  exit 1
fi

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

printf '%s\n' "$owned" | while IFS= read -r destination; do
  [ -n "$destination" ] || continue
  if [ -L "$destination" ]; then
    rm "$destination"
  fi
done

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
rm -f "$manifest"

echo "Removed this SightMesh installation and its owned local links."
echo "Durable state under ~/.local/share/sightmesh and ~/.local/state/sightmesh is kept; delete it explicitly to finish."
