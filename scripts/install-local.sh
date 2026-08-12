#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

uv tool install --editable "$repo_root" --force

link_skill() {
  skill_name=$1
  source_path="$repo_root/skills/$skill_name"
  for skill_root in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
    mkdir -p "$skill_root"
    destination="$skill_root/$skill_name"
    if [ -L "$destination" ] && [ "$(readlink "$destination")" = "$source_path" ]; then
      continue
    fi
    if [ -e "$destination" ] || [ -L "$destination" ]; then
      echo "Refusing to replace existing skill: $destination" >&2
      exit 1
    fi
    ln -s "$source_path" "$destination"
  done
}

link_skill orchestrate-visible-agents
link_skill reconcile-agent-work

echo "Installed agent-deck and linked shared Claude/Codex skills."
