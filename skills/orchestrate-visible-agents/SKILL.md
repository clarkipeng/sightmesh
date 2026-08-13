---
name: orchestrate-visible-agents
description: Launch and coordinate full Claude Code and Codex workers as visible local cdesktop sessions. Use for delegation, parallel work, review, or multi-agent coordination.
---

# Orchestrate Visible Agents

Delegate through cdesktop so workers, transcripts, files, and lifecycle remain visible. Do not use hidden native subagents.

## Shape the work

- Continue an existing owner before creating another.
- Isolate writing workers in worktrees. Share a workspace only for read-only or clearly disjoint work.
- Give each conflict hotspot one writer and parallelize independent results.

Before launch, verify the canonical repository, exact base, ownership, and one useful check. Sequence setup that mutates shared repository or control state, then run the isolated workers in parallel. Keep the assignment short: objective, scope, proof, delivery, and stop condition.

```sh
sightmesh spawn --name worker --repo <root> --base <branch> \
  --profile <profile> --prompt-file <file> --worktree --unattended
```

## Communicate by consequence

Queue information when current work remains valid. Steer only when waiting would make the work wrong or unsafe.

```sh
sightmesh message @worker --message "New evidence: <path or SHA>"
sightmesh steer @worker --message "Stop using the old base; use <SHA>"
```

Use `.context` for durable task-local handoffs, Git for source truth, cdesktop for live state, and Repowire for cross-workspace contact. Avoid duplicate transcript or context stores.

## Supervise and finish

Inspect on meaningful state changes, not continuous polling. Intervene for blockers, drift, duplicate ownership, or failed proof. Make proof run the owner of each changed artifact; an adjacent check is not equivalent. Batch independent questions and reads when useful.

Before retirement, use `$reconcile-agent-work`. Preserve or explicitly defer every unique result, then archive the visible workspace.
