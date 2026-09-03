# Agent instructions

Read [TASTE.md](TASTE.md) first: the founder-set taste that decides judgment calls (proceed without asking, models by role, reviews by blast radius, credentials rotate, and the rest).
This file holds only what is specific to SightMesh. When the two seem to disagree, TASTE.md wins and this file gets fixed.

Use `$orchestrate-visible-agents` for every delegated assignment. Do not use native hidden subagents. Use `$reconcile-agent-work` before changing ownership, provider, or lifecycle.

Keep orchestration local. Do not add credential extraction, auth-header replay, or rate-limit evasion. Selecting among accounts the operator owns and has logged into normally is supported: observe quota and move to the next account, using each account's own credentials.

Keep the operator model harness-native and minimal. `.context` is workspace-local, cdesktop owns transcripts and visible sessions, Git owns worktrees and source state, and Repowire owns cross-workspace contact. Do not add a global context mirror, transcript copy, custom MCP, or new command when ordinary files, Git, cdesktop, or Repowire already provide the capability.

## Working defaults

- Workers run unattended (BYPASS_PERMISSIONS) in their own worktree; nothing waits on a human approval. Reserve supervised policies for destructive actions (merge, deploy, delete, restart, migrate); never give a worker a permission that forbids its own deliverable (plan-only workers cannot write reports or run `sightmesh complete`).
- Launch admission is kernel-side (`SIGHTMESH_MAX_ACTIVE_WORKERS`, default 4). Do not hand-throttle around it.
- Resumable by default: append partial results to `<output>.partial.md` and run `sightmesh checkpoint` every ~15 minutes; a replacement reads the partial first.
