---
name: orchestrate-visible-agents
description: Launch and coordinate full Claude Code and Codex workers as visible local cdesktop sessions, with Repowire communication across workspaces. Use whenever an agent is asked to delegate, parallelize, spawn a worker, create a subagent, coordinate another coding agent, or divide implementation and review work. Enforces that native hidden subagents are never used for delegated work.
---

# Orchestrate Visible Agents

Run every delegated assignment as a first-class cdesktop session. Preserve a live transcript, explicit ownership, and a human-visible lifecycle.

## Hard invariant

Do not use the host agent's native subagent, Task, delegate, fork, or team mechanism. Do not hide delegated work inside the parent transcript. Use cdesktop even if a native mechanism seems faster.

Keep the workflow native and unsurprising:

- use ordinary workspace-local `.context` files for durable handoffs;
- keep `.context` handoffs ignored and untracked unless the repository explicitly declares that exact file tracked; never force-add an ignored handoff, and verify it is absent from the candidate tree before push or PR handoff;
- use Git and the filesystem to inspect sibling worktrees;
- use cdesktop for session rosters, transcripts, and human interaction;
- use `sightmesh peers`, `peek`, and `steer` for compact fleet awareness and immediate targeted contact;
- use Repowire for durable cross-workspace asks and replies that should not interrupt the target;
- do not create a global context mirror, duplicate transcripts, or introduce another MCP or command when these surfaces already suffice.

## Choose the topology

1. Use a separate cdesktop workspace with an isolated worktree for implementation, independently shippable changes, or overlapping repository access.
2. Use `cdesktop team spawn` only for read-only review, research, or disjoint paths in the same workspace.
3. Keep one lead session per cdesktop workspace. Only the lead spawns teammates.
4. Any visible agent may contact any other. Use `sightmesh message @agent --message "..."` to queue information without interrupting valid work; use `sightmesh steer @agent --message "..."` when delay would cause invalid output, unsafe mutation, or avoidable rework. Use Repowire when the ask needs durable cross-workspace delivery.
5. Every child must use `sightmesh parent --message "..."` for a blocker, decision, or completion that must wake its launcher. Same-workspace teammates may use `cdesktop team manager` as the native lead alias.
7. Before asking for input, collect all currently known independent questions and send one multi-question request. Before tool use, batch independent read-only inspections. Keep dependent operations, mutations, approvals, and destructive actions sequential.
8. A lead reviewing multiple workers should call `sightmesh inbox` once and answer independent pending requests with one prevalidated `sightmesh respond --responses '<json>'` call.

Read [references/cdesktop-and-repowire.md](references/cdesktop-and-repowire.md) before the first spawn in a new environment.

## Define the assignment

Before spawning, write a bounded prompt containing:

- objective and non-goals;
- exact repository, an existing base branch, and the frozen 40-character SHA in the prompt when correctness depends on it;
- owned paths and forbidden paths;
- expected artifact, checks, branch, PR state, and completion marker;
- handoff location, stated as a path inside the worker's own checkout; require the worker to resolve it against its own verified working directory, never against the canonical repository source, and verify after completion that nothing was written under the canonical root;
- instruction to use this skill for any further delegation;
- instruction to use `$reconcile-agent-work` before completion.

Keep ordinary worker prompts under 250 words. Link to repository authority and ignored handoffs instead of copying their prose, matrices, history, or generic workflow. Include a detail only when the worker cannot discover it from the named files or when getting it wrong would change the output. Use short labeled lines or bullets so objective, ownership, exclusions, proof, delivery, and stop condition are visible at a glance.

Never satisfy a handoff requirement by force-adding an ignored `.context` file. Before the final push, run `git check-ignore <handoff>` and `git ls-files --error-unmatch <handoff>`; the first must succeed and the second must fail unless repository authority explicitly declares that exact handoff tracked.

Search cdesktop and Repowire inventories first. Do not spawn a duplicate worker for a branch, PR, or assignment already owned.

Before spawning, run a bounded preflight against the exact base: confirm the branch resolves to the intended SHA, determine whether the new worktree will have required dependencies, verify any required local service, and name one focused command that can execute there. Put the delivery branch and push target in the prompt when SightMesh will create a different local branch. If setup is missing, provision it once through the repository's supported bootstrap path or state the exact setup command in the assignment. Do not make each worker rediscover the same missing dependency or service.

Resolve `--repo` to the canonical repository root before every isolated spawn. Use the first `worktree` entry from `git worktree list --porcelain`, verify it is not inside `.cdesktop-workspaces`, and pass that exact path. Never pass the current managed checkout or another worker's worktree as `--repo`; doing so creates a duplicate repository group and can let child setup mutate the parent's checkout. After spawning, inspect `sightmesh peek` and require its `source` to equal the canonical root before authorizing writes.

When taking over a workspace imported from Conductor, check for `.context/sightmesh-migration.json`. Read the referenced handoff before writing, then validate the live branch, HEAD, dirty state, and remaining scope against it. The original checkout and Conductor database remain authoritative historical sources.

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

Match the model family to the executor before creating or using a profile. Fable, Opus, Sonnet, and Haiku require `CLAUDE_CODE`; GPT and Codex model IDs require `CODEX`. A provider listing a model does not prove every executor can run it. After launch, require at least one assistant message or tool call; a session that completes with neither is a failed launch, not completed work.

## Communicate

Start with the compact local fleet surface:

```sh
sightmesh peers
sightmesh peek @<agent>
sightmesh message @<agent> --message "<non-interrupting information>"
sightmesh steer @<agent> --message "<immediate correction or blocker>"
sightmesh parent --message "<status, decision request, or blocker>"
sightmesh inbox
```

Selectors are exact and ambiguity-safe. Never steer yourself. Targeted steering leaves other sessions in the same workspace and any dev server running. It refuses to interrupt a pending approval or question.

Use Repowire for peer discovery, asks, and delivery tracing when the peer reports online:

```sh
repowire peer list -a
repowire peer ask <peer-name> "<bounded request>"
```

For cdesktop, target the online bridge peer whose repository path and backend match the intended session. Repowire chooses the displayed name. Do not target a stale offline identity created by an executor's short-lived app-server hook. If no matching bridge peer is online, use `sightmesh steer` for direct agent contact and report that Repowire delivery was unavailable.

Send a peer message only when it changes the recipient's action or resolves a decision. Keep it to the minimum actionable delta, usually `DONE head=<sha> checks=<result> handoff=<path>`, `BLOCKED cause=<cause> need=<decision>`, or one direct instruction. Put narrative context and evidence in the ignored handoff. Repowire is transport, not the sole record.

Before a worker completes, require every finding, blocker, exact SHA, validation result, and next action needed by another workspace to appear in the ignored handoff itself. A terminal response or transcript may summarize or link to that handoff, but must not be the only place containing actionable detail.

## Review plans

Use SightMesh's cdesktop-native approval commands instead of answering for a worker in its transcript:

```sh
sightmesh approval list
sightmesh approval show <approval-id>
sightmesh approval approve <approval-id>
sightmesh approval reject <approval-id> --reason "<bounded reason>"
```

An agent must never approve its own request. When invoked from cdesktop, only the lead session in the reviewer workspace may decide another session's plan. Questions remain interactive in cdesktop. Do not approve a non-plan tool request unless the assignment explicitly authorizes it and `--allow-non-plan` is present. Record significant plan changes in the owning workspace's `.context` handoff.

## Supervise and finish

Inspect cdesktop transcripts and derived git state. Intervene only on state change, a blocker, scope drift, duplicate ownership, or failed validation. Before retirement, invoke `$reconcile-agent-work`. Archive only after the branch, PR, dirty state, handoff, and remaining scope are reconciled.

Keep one writer for each conflict hotspot such as a migration or shared composition file. Parallelize disjoint fixtures, adapters, new test files, docs, and exact-head review. Prefer short pushed checkpoints with explicit remaining scope, and replace a worker from the exact pushed branch before context pressure degrades judgment.

Do not rotate an agent at a fixed context percentage. Rotate when behavior shows degraded state tracking or instruction quality, or when the remaining context is plainly insufficient for the next bounded phase. Otherwise preserve continuity.
