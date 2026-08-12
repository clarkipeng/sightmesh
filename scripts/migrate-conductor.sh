#!/bin/zsh
set -euo pipefail

plan_output="$(sightmesh --json migrate plan "$@")"
print -r -- "${plan_output}"
plan_path="$(print -r -- "${plan_output}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["plan_path"])')"

print -r -- ""
print -r -- "Plan created. Review it before applying:"
print -r -- "  ${plan_path}"
print -r -- ""
print -r -- "When every selected Conductor session is paused, apply active workspaces with:"
print -r -- "  sightmesh migrate apply '${plan_path}' --all --confirm-conductor-paused"
print -r -- ""
print -r -- "Archived and dirty sources require the additional explicit flags documented in docs/migration.md."
