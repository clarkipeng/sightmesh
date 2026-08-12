---
name: reconcile-agent-work
description: Checkpoint, hand off, recover, and retire visible Claude Code or Codex work safely. Use when an agent finishes, stalls, reaches a model or rate limit, changes executor or provider, transfers ownership, closes a cdesktop teammate or workspace, or when a manager reconciles branches, PRs, transcripts, dirty files, and remaining scope.
---

# Reconcile Agent Work

Turn a running agent assignment into a durable, auditable state before changing ownership or lifecycle.

## Checkpoint

Record the following in the narrow task handoff file:

- objective, current owner, workspace and session IDs;
- repository path, branch, exact HEAD, base, and upstream;
- PR number and draft or review state;
- delivered artifacts and checks with exact results;
- dirty, untracked, unpushed, or unmerged work;
- open feedback, blockers, deferred scope, and next action;
- last known Repowire peer identity;
- whether the worker may be archived.

Use [references/handoff-template.md](references/handoff-template.md) when no owning format exists.

## Handle capacity exhaustion

Read [references/capacity-and-credentials.md](references/capacity-and-credentials.md) before changing an executor, model, provider, or login.

On a rate or context limit:

1. Stop issuing new model requests to the exhausted worker.
2. Request or reconstruct a checkpoint from Git, cdesktop, Repowire, and the PR host.
3. Mark the exact last completed action and any command with unknown outcome.
4. Select only a provider profile or API credential the user explicitly configured through supported vendor mechanisms.
5. When the destination is an approved SightMesh API or enterprise profile, run `sightmesh failover WORKSPACE_ID --profile NAME --checkpoint-file FILE`. This starts a visible successor in the same workspace by default, preserving dirty files and transcript context. Use `--new-worktree` only for a clean committed handoff.
6. Otherwise launch or resume a visible cdesktop session and give it the checkpoint.
7. Record the ownership transition and validate the first read-only inventory before authorizing writes.

Never extract, replay, share, or rotate browser cookies, auth headers, refresh tokens, session tokens, keychain records, or consumer-subscription credentials. Never cycle accounts to evade vendor rate limits or usage controls.

## Reconcile before retirement

Compare:

- original assignment and later follow-ups;
- cdesktop transcript and session roster;
- Repowire asks and delivery traces;
- repository status, untracked files, branch, upstream, and worktrees;
- PR diff, comments, reviews, checks, and merge state;
- deployed state when the assignment included deployment.

Classify every requested item as delivered, explicitly deferred with owner, blocked with evidence, or missing. Do not archive while unique work or an unresolved ownership conflict remains.

## Close visibly

First request closeout from the lead session:

```sh
sightmesh close <workspace-id> --message-file <closeout-prompt>
```

After the response is complete and reconciliation passes:

```sh
sightmesh close <workspace-id> --archive --confirm-reconciled
```

Archiving stops the workspace and hides it from the active list while preserving cdesktop's recorded history. Do not delete the worktree, branch, transcript, or handoff as part of ordinary retirement.

The CLI refuses dirty repositories. If dirty state is intentionally preserved and fully recorded in the handoff, add `--preserve-dirty`. Never use that flag merely to silence the guard.
