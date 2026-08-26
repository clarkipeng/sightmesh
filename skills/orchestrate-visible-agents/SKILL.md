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

One manager owns the complete roster and final reconciliation. Every assignment names one objective, owner, exact repository, base, branch and upstream, owned paths, proof gates, authority limits, handoff recipient, and stop condition. Before launch, verify those facts; sequence shared mutations, then run isolated work in parallel.

```sh
sightmesh spawn --name worker --repo <root> --base <branch> \
  --profile <profile> --prompt-file <file> --worktree --unattended
```

## Prove long-running launches

Before a long-running or paid launch, the owning runner proves checkpoint recovery, append-only exact retry, preservation of prior terminal results, and one writer per persistent output root. It records plan identity, output root, retry identity, spend basis, and any spend limit.

The supervisor must outlive the initiating turn and have a verified terminal wake or callback. A policy prompt cannot prove external-process supervision: when that wake path is absent, do not call the run supervised; record the product gap (see #55) for the runner instead. Each runner repository owns and tests its domain-specific retry semantics.

## Communicate by consequence

Queue information when current work remains valid. Steer only when waiting would make the work wrong or unsafe.

```sh
sightmesh message @worker --message "New evidence: <path or SHA>"
sightmesh steer @worker --message "Stop using the old base; use <SHA>"
```

Use `.context` for durable task-local handoffs, Git for source truth, cdesktop for live state, and Repowire for cross-workspace contact. Avoid duplicate transcript or context stores.

## Supervise and finish

Inspect on meaningful state changes, not continuous polling. Intervene for blockers, drift, duplicate ownership, failed proof, or a missing wake. Make proof run the owner of each changed artifact; an adjacent check is not equivalent. Batch independent questions and reads when useful.

Treat peer, parent, and approval messages as interruptions, not completion. After responding, resume the owned task unless the message replaces or invalidates it.

A checkpoint is progress, not completion. If owned scope remains, continue it or start its successor before ending.

Before retirement, use `$reconcile-agent-work`. Preserve or explicitly defer every unique result, then archive the visible workspace.
