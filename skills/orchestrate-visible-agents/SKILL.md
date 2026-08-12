---
name: orchestrate-visible-agents
description: Launch and coordinate full Claude Code and Codex workers as visible local cdesktop sessions, with Repowire communication across workspaces. Use whenever an agent is asked to delegate, parallelize, spawn a worker, create a subagent, coordinate another coding agent, or divide implementation and review work. Enforces that native hidden subagents are never used for delegated work.
---

# Orchestrate Visible Agents

Run every delegated assignment as a first-class cdesktop session. Preserve a live transcript, explicit ownership, and a human-visible lifecycle.

## Hard invariant

Do not use the host agent's native subagent, Task, delegate, fork, or team mechanism. Do not hide delegated work inside the parent transcript. Use cdesktop even if a native mechanism seems faster.

## Choose the topology

1. Use a separate cdesktop workspace with an isolated worktree for implementation, independently shippable changes, or overlapping repository access.
2. Use `cdesktop team spawn` only for read-only review, research, or disjoint paths in the same workspace.
3. Keep one lead session per cdesktop workspace. Only the lead spawns teammates.
4. Use Repowire for communication between different cdesktop workspaces.
5. Use `sightmesh prompt-idle` when a script or manager must prompt only an idle session. A normal `sightmesh message` is allowed when delivery must queue behind an active turn.

Read [references/cdesktop-and-repowire.md](references/cdesktop-and-repowire.md) before the first spawn in a new environment.

## Define the assignment

Before spawning, write a bounded prompt containing:

- objective and non-goals;
- exact repository, an existing base branch, and the frozen 40-character SHA in the prompt when correctness depends on it;
- owned paths and forbidden paths;
- expected artifact, checks, branch, PR state, and completion marker;
- handoff location;
- instruction to use this skill for any further delegation;
- instruction to use `$reconcile-agent-work` before completion.

Search cdesktop and Repowire inventories first. Do not spawn a duplicate worker for a branch, PR, or assignment already owned.

## Spawn visible workers

For isolated implementation:

```sh
sightmesh spawn \
  --name <name> \
  --repo <repository-root> \
  --base <existing-branch> \
  --executor <CLAUDE_CODE|CODEX> \
  --prompt-file <prompt-file> \
  --worktree \
  --unattended
```

For a read-only teammate in the current cdesktop workspace:

```sh
sightmesh teammate-spawn --name <name> --prompt-file <prompt-file>
```

To mix Claude and Codex in one workspace, pass `--executor`, `--model`, and `--provider` as required by cdesktop. Never synthesize or extract provider credentials.

Prefer named SightMesh profiles over raw provider IDs when repeatability matters. Inspect only redacted provider metadata with `sightmesh --json profile providers`. Profiles contain identifiers and policy, never keys or tokens.

## Communicate

Within one workspace:

```sh
sightmesh teammate-list
sightmesh message <session-id> --message-file <message-file>
```

Across workspaces, use the visible cdesktop session ID for immediate routing:

```sh
sightmesh list
sightmesh message <session-id> --message-file <message-file>
```

Use Repowire for peer discovery, asks, and delivery tracing when the peer reports online:

```sh
repowire peer list -a
repowire peer ask <peer-name> "<bounded request>"
```

For cdesktop, target the online bridge peer whose repository path and backend match the intended session. Repowire chooses the displayed name. Do not target a stale offline identity created by an executor's short-lived app-server hook. If no matching bridge peer is online, use `sightmesh message` and report that Repowire delivery was unavailable.

Use messages for decisions, exact SHAs, ownership transfers, and blockers. Keep durable evidence in the repository or its ignored handoff directory. Repowire is transport, not the sole record.

## Supervise and finish

Inspect cdesktop transcripts and derived git state. Intervene only on state change, a blocker, scope drift, duplicate ownership, or failed validation. Before retirement, invoke `$reconcile-agent-work`. Archive only after the branch, PR, dirty state, handoff, and remaining scope are reconciled.
