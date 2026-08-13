---
name: orchestrate-visible-agents
description: Launch and coordinate full Claude Code and Codex workers as visible cdesktop sessions with SightMesh and Repowire. Use for delegation, parallel work, review, or multi-agent coordination.
---

# Orchestrate Visible Agents

Delegate through visible cdesktop sessions. Keep ownership, transcripts, Git state, and handoffs inspectable. Do not use hidden native subagents.

Read [references/cdesktop-and-repowire.md](references/cdesktop-and-repowire.md) before the first spawn in a new environment.

## Choose the shape

- Use an isolated worktree for implementation or independently shippable work.
- Use a same-workspace teammate for read-only review or truly disjoint files.
- Keep one writer for each conflict hotspot. Parallelize independent adapters, fixtures, docs, and reviews.
- Search the current fleet first. Continue an existing owner instead of spawning a duplicate.
- Any agent may contact any other agent.

## Launch safely

Before a spawn, verify the exact base SHA, canonical repository root, dependencies, required services, owned paths, delivery branch, and one focused check. Fix shared bootstrap problems once.

Use the canonical repository root from the first normal entry in `git worktree list --porcelain`, never another managed worker checkout.

```sh
sightmesh spawn --name <name> --repo <canonical-root> --base <branch> \
  --profile <profile> --prompt-file <file> --worktree --unattended
```

Match executor and model families. Prefer named profiles. Never copy provider credentials into prompts or files. A launch is healthy only after an assistant message or tool call appears.

## Write compact assignments

Keep ordinary prompts under 250 words. Link to authority instead of repeating it. Include only facts whose absence could change the result:

```text
AUTHORITY <file or contract>  BASE <40-char SHA>
OBJECTIVE <one result>
OWNER <paths or external state>
EXCLUDE <nearby work owned elsewhere>
PROOF <focused checks and review>
DELIVER <branch, draft PR, ignored handoff>
STOP <pushed checkpoint, verdict, or concrete blocker>
```

Require `.context` handoffs to be resolved inside the worker checkout, ignored, and untracked. Include the exact manager selector unless `sightmesh parent` is proven to resolve the launcher. Require `$reconcile-agent-work` before retirement.

## Communicate by consequence

Queue information while the current course remains valid:

```sh
sightmesh message @agent --message "DONE head=<sha> checks=<result> handoff=<path>"
```

Steer only when delay would cause invalid output, unsafe mutation, scope drift, or avoidable rework:

```sh
sightmesh steer @agent --message "STOP old head. Review <new SHA> only."
```

Use concise freeform prose when reasoning or ambiguity matters. Put durable evidence, decisions, blockers, and remaining scope in the handoff. Treat messages as routing, not the sole record. When a summary conflicts with Git or the handoff, inspect the exact head and handoff.

Use Repowire for durable cross-workspace asks when the correct bridge peer is online. Use SightMesh for direct local contact. Send messages only when they change the recipient's action or judgment.

## Supervise

Inspect `sightmesh peers`, `peek`, Git, and handoffs at meaningful state changes. Prefer worker-triggered wakes and bounded one-shot checks. Avoid unmanaged polling loops, redundant status traffic, and repeated CI reads.

Intervene on blockers, failed checks, duplicate ownership, drift, or degraded judgment. Rotate when continuity is no longer reliable, not at a fixed context percentage. Resume from the exact pushed checkpoint and concise handoff. Use one-step `sightmesh failover` only when a compatible approved profile supports it.

For plans and approvals, use SightMesh approval commands. Agents never approve their own requests.

## Finish

Before archiving, run `$reconcile-agent-work` and verify the branch, PR, exact head, dirty files, ignored handoff, checks, merged result, and remaining scope. Preserve unique work or explicitly defer it. Archive only after the result is acknowledged and recoverable.
