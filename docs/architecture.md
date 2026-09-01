# Architecture

SightMesh is a thin local policy layer over native owners. Its purpose is to make full Claude Code and Codex workers visible, interruptible, isolated, and recoverable without inventing a second agent runtime.

## Native ownership

| Concern | Owner |
| --- | --- |
| Sessions, transcripts, processes, approvals, and durable commands | cdesktop |
| Branches, checkouts, and worktrees | Git |
| Workspace-local handoffs | Git-ignored `.context` files |
| Cross-workspace request/reply transport | Repowire |
| Credential-pool order, local leases, and lifecycle policy | SightMesh |

This boundary is intentional. SightMesh validates operator intent and calls the native owner; it does not mirror all transcripts, source state, or relationships into a global database.

## Visible sessions and isolation

`sightmesh spawn` creates a cdesktop workspace and full provider session. The normal implementation path uses a dedicated Git worktree, allowing independent workers from one repository to coexist. Direct-checkout work is supported but conflicts with other ownership for that repository and cannot use unattended mode.

cdesktop remains the inspection surface: conversation, process output, changed-file diffs, browser preview, and interruption are visible to the operator. A session is not a background prompt hidden behind SightMesh.

## Durable commands

Prompts, follow-ups, UI actions, and bridged messages enter cdesktop's durable command path. A `continue` command waits for an idle boundary. A `replace` command requests cancellation of the active coding turn and then runs through the same dispatcher. Command state and execution ownership are recorded before more work is admitted for that session.

Successor routing is append-only and acyclic. SightMesh validates the complete proposed successor chain in the same serialized transaction that records an edge, so a direct or transitive self-successor is rejected without changing durable ownership state.

### Lifecycle delivery safety contract

SightMesh and cdesktop enforce the same lifecycle boundary from opposite sides:

1. SightMesh attributes every child lifecycle command to the child session that caused it.
2. SightMesh never treats a lifecycle command as a new child event.
3. SightMesh resolves invalid self or successor routes once and records that terminal decision before another reconciliation tick.
4. cdesktop rejects any peer command whose sender and recipient are the same before it creates a command row or execution.
5. cdesktop owns exact-once managed task effects and rejects a reused or stale task epoch.

The two independent consumers are SightMesh's durable reconciler and cdesktop's follow-up endpoint. The shared field is the existing sender session header, not a new orchestration ID a human must supply. A malformed graph, duplicate wakeup, bridge restart, or response loss can therefore produce at most one native effect for the same logical event.

Managed launch attempts remain limited by the task's fixed attempt budget. Codex rollout size and free-disk guards belong to cdesktop, where they apply even if SightMesh fails. SightMesh-managed service logs rotate at a fixed byte limit so logging cannot bypass that storage boundary.

Durability here means the command record survives a process boundary and can be reconciled. It does not yet prove that every manager wake and delivery is acknowledged during prolonged real-world load. That is the current experimental release gate.

## Stall and recovery model

The managed bridge watches spawned children for event-snapshot silence and surfaces stalls. Recovery preserves the native evidence first: the visible transcript, Git worktree and branch, durable command state, and optional `.context` handoff. Parent-session links provide a return address for status or escalation; they are not an authority hierarchy.

Updates drain the fleet at a safe boundary rather than claiming to preserve an active model stream across a backend restart. Failover starts an explicit successor using a configured provider profile; it does not silently move credentials between accounts.

## Honest limitations

- SightMesh is experimental and has no production reliability or support SLA.
- Durable manager wake and acknowledged delivery still need several weeks of proof under real load before active promotion.
- Local agents can run arbitrary commands with the permissions granted to their provider CLI. Worktrees reduce source collisions; they are not a security sandbox.
- The cdesktop fork, Repowire, provider CLIs, Git, and local machine are all part of the failure and trust boundary.
- Provider quota visibility is incomplete. Claude setup tokens and API keys are reported as unknown rather than inferred.
- Restarting the backend interrupts an active executor stream. Recovery resumes from preserved state; it is not transparent continuation.
- Pooling is only for accounts the operator owns and authenticated through normal provider flows. SightMesh does not support credential extraction, session sharing, or rate-limit evasion.

Deep operational contracts live in [Operations](operations.md), [Storage and retention](storage.md), and [Compatibility](compatibility.md).
