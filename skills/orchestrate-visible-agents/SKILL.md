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

Read [references/cdesktop-and-repowire.md](references/cdesktop-and-repowire.md) before the first spawn in a new environment.

## Define the assignment

Before spawning, write a bounded prompt containing:

- objective and non-goals;
- exact repository and base ref, including the 40-character SHA when correctness depends on it;
- owned paths and forbidden paths;
- expected artifact, checks, branch, PR state, and completion marker;
- handoff location;
- instruction to use this skill for any further delegation;
- instruction to use `$reconcile-agent-work` before completion.

Search cdesktop and Repowire inventories first. Do not spawn a duplicate worker for a branch, PR, or assignment already owned.

## Spawn visible workers

For isolated implementation:

```sh
agent-deck spawn \
  --name <name> \
  --repo <repository-root> \
  --base <exact-ref> \
  --executor <CLAUDE_CODE|CODEX> \
  --permission SUPERVISED \
  --prompt-file <prompt-file> \
  --worktree
```

For a read-only teammate in the current cdesktop workspace:

```sh
agent-deck teammate-spawn --name <name> --prompt-file <prompt-file>
```

To mix Claude and Codex in one workspace, pass `--executor`, `--model`, and `--provider` as required by cdesktop. Never synthesize or extract provider credentials.

## Communicate

Within one workspace:

```sh
agent-deck teammate-list
agent-deck message <session-id> --message-file <message-file>
```

Across workspaces, use the visible cdesktop session ID for immediate routing:

```sh
agent-deck list
agent-deck message <session-id> --message-file <message-file>
```

Use Repowire for peer discovery, asks, and delivery tracing when the peer reports online:

```sh
repowire peer list -a
repowire peer ask <peer-name> "<bounded request>"
```

Use messages for decisions, exact SHAs, ownership transfers, and blockers. Keep durable evidence in the repository or its ignored handoff directory. Repowire is transport, not the sole record.

## Supervise and finish

Inspect cdesktop transcripts and derived git state. Intervene only on state change, a blocker, scope drift, duplicate ownership, or failed validation. Before retirement, invoke `$reconcile-agent-work`. Archive only after the branch, PR, dirty state, handoff, and remaining scope are reconciled.
