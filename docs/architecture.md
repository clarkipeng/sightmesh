# SightMesh v0.9 architecture

SightMesh is a thin local control surface over cdesktop. It does not own a second scheduler, transcript, relationship graph, delivery queue, or worktree registry.

## Design rule

Encode semantics and invariants, not catalogs of edge cases. Prefer one general mechanism with a small example over several special commands or recovery paths.

## Ownership

- cdesktop owns workspaces, sessions, commands, executions, relationships, archives, and resource admission.
- Git owns source, branches, and worktrees.
- Repowire transports cross-workspace messages.
- `.context` holds ignored workspace-local handoffs.
- SightMesh validates intent and calls those owners.

SightMesh keeps no long-running updater and no runtime SQLite databases.

## Command schema

Every prompt source writes the same durable record:

```sql
session_commands (
  id                    BLOB PRIMARY KEY,
  session_id            BLOB NOT NULL REFERENCES sessions(id),
  dedupe_key            TEXT UNIQUE,
  intent                TEXT NOT NULL CHECK (intent IN ('continue', 'replace')),
  body                  TEXT NOT NULL,
  config                TEXT,
  state                 TEXT NOT NULL CHECK (
                          state IN ('pending', 'claimed', 'done', 'failed', 'cancelled')
                        ),
  execution_process_id  BLOB REFERENCES execution_processes(id),
  created_at            TEXT NOT NULL,
  finished_at           TEXT
)
```

`sessions.parent_session_id` stores optional spawn lineage. A partial unique index permits at most one running coding execution per session.

## Scheduler

The dispatcher is the only component that starts agent turns.

1. Append a command and wake the dispatcher.
2. Claim pending commands only when the session is idle and fleet capacity is available.
3. Assign all commands claimed together to one execution, preserving order.
4. Record terminal state before admitting more work for that session.

The same path handles UI prompts, peer messages, resumed work, and Repowire delivery. A duplicate `dedupe_key` returns the existing record. `replace` requests cancellation of the active turn, then runs through the same dispatcher. Restart recovery reads durable command and execution state; it never guesses from process timing.

The scheduler admits work under a configurable concurrency cap and pauses admission under operating-system memory pressure. Resource accounting includes the agent process group, so tools and builds count with their parent worker. Critical eviction preserves the command, transcript, Git state, and visible workspace for explicit retry.

## Lifecycle

- Spawn creates the workspace, session, optional parent link, and first command as one operation.
- Archive and restore use cdesktop state directly. Clean worktree reclamation is scheduled for the exact expiry or checked at startup, not polled.
- Updates are explicit: verify, require idle, replace, and health-check. No updater daemon or retry loop.
- The UI uses event streams for fleet, command, approval, and update state.

## SightMesh surface

Keep only high-value local verbs: service, open, status, spawn, message, steer, peers, peek, approvals, workspace lifecycle, migrate, and explicit update. Prefer cdesktop or Git directly when they already express the operation.

## Replacement boundary

v0.9 requires the scheduler-capable cdesktop release. Remove the v0.8 delivery, relationship, lease, route, bridge, and updater stores instead of maintaining dual behavior. Migration imports only durable information that has no native owner, then archives the old private state for explicit deletion.
