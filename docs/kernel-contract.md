# Kernel contract v1

One durable task kernel replaces broker semantics.
Every clause names its owner: kernel (SightMesh), executor (cdesktop), or simulator.
A release of either side ships only when the simulator suite pinned to this contract version passes.
Consumers: the existing CLI, Python SDK, and structured tools; their public surface does not change.

## Task kernel (owner: kernel)

Identity is the semantic `task_key` plus `parent_task_key`.
UUIDs, paths, and session IDs are diagnostic output only; no caller supplies them.

One row per task: key, parent, state, epoch, holder session, budgets (`max_children`, `max_attempts`), wait predicate, result manifest reference.

States: `pending -> active -> (awaiting_review | blocked(reason) | lost) -> completed -> archived -> reclaimed`.
Terminal transitions are monotone: the first legal terminal transition wins.
Every mutation carries the task version it observed and fails on mismatch.
A completed model turn is never a completed task; only an explicit lifecycle call or a satisfied predicate advances state.

One holder per `(task, epoch)`.
Replacement requires an explicit `transfer(reason)` that fences the old epoch before any new session launches.
Self-parent links and destination-equals-self deliveries are unrepresentable (schema constraints), not rejected at runtime.
Budgets count durable child and journal rows, never in-memory intentions.

## Effects journal (owner: kernel)

The journal is the only code path that launches anything.
An effect is `INSERT (task_key, epoch, request_hash)` under a unique index; a duplicate insert returns the existing effect.
Effect lifecycle: `reserved` (expiring lease, owner-fenced) -> `launched` (workspace, session) -> typed terminal.
An expired reservation is adoptable or retriable with the reserved identifiers; it can never orphan a native session or fork a duplicate.

## Mailbox (owner: kernel)

Three lanes.
Human: append-only, ordered, never machine-cleaned.
Status: one replaceable latest entry per worker key.
Cohort: child results accumulate; the cohort produces wakes, not per-child mail.

Every send has an acknowledgment state (`queued | delivered | acted | rejected | expired`) and a TTL.
Poison rows park once with a dead-letter reason and are never re-polled.
Terminal sessions refuse machine mail; human mail addressed to a terminal task routes to the attention queue.
Worker results carry a small manifest: task, outcome, artifacts, checks, continuation key.

## Wakes (owner: kernel)

Managers wait on durable predicates: `all_children_terminal`, `any_child_blocked`, or `external_receipt_present`.
Liveness predicates `any_child_lost`, `any_child_stalled`, and `any_child_over_budget` extend this set; their detection rules, episode dedupe, and executor prerequisites are specified in `liveness-spec.md`.
The kernel never kills and never respawns on a liveness signal; it wakes the owning manager.
One wake per satisfied predicate, claimed atomically under a lease; delivery is at-least-once and idempotent by predicate identity.
A reconciler scans tasks (not commands) for satisfied-but-undelivered predicates, repairing any crash between state change and notification.
Lifecycle continuation has bounded latency and cannot be starved behind ordinary backlog.

## Routing (owner: kernel)

Two route classes: `standard` (terra -> luna -> sol) and `deep` (fable -> opus -> sol).
Explicit profile overrides are allowed and remain recoverable.
Only typed provider outcomes (`quota{retry_at} | auth | provider_down`) advance a chain, with per-account cooldown; repository, test, and code failures never reroute.
`routing validate` proves a usable path before dispatch.
Admission enforces a launch rate bound and reserves host process headroom before any fork.

## Approvals (owner: kernel policy, executor enforcement)

Default execution mode accepts edits and tool calls.
Supervision applies only to destructive lifecycle actions: merge, deploy, delete, restart, migrate.
A parked approval releases its execution slot; a durable decision resumes the same turn.
Timeout produces `blocked(approval)`, never a killed process.
Read-only command patterns get narrow reusable grants scoped to a repository.

## Executor seam (owner: executor)

`PUT /task-launches/{task}/{epoch}` is create-or-return; the body is the native launch spec; the executor computes and stores the request hash.
`GET` returns `{state, workspace_id, session_id}`.
The executor never decides task completion, retry, or replacement.

Terminal outcomes are typed: `completed | blocked(reason) | lost`.
Completion is never inferred from idle time; stream death and process death reconcile against each other; each slot is released exactly once.

All limits are enforced where bytes are written: transcript size, fork count, log size (at the open file descriptor), and free-disk reserve.
Crossing a limit terminates the owned process tree and reports `blocked(limit)`.
Successors receive a content-addressed history reference; transcript copying does not exist.

Workspace lists are metadata-only.
Fresh Git truth requires an explicit single-workspace refresh behind a small global subprocess semaphore; superseded refreshes cancel.
Capabilities are executed probes at `doctor` time; an advertised capability without a passing probe fails `doctor`.

## Retention (owner: split as stated)

Machine mail and resolved commands expire on a TTL (kernel).
Approval and escalation stores keep bounded windows (kernel).
Logs are bounded at the writer (executor).
Archived worktrees, including build caches, are reclaimed after a TTL, only after transactional closeout (executor, gated by kernel state).
`submit` requires a pushed branch or committed artifacts; closeout refuses untracked deliverables (kernel).

## Observability (owner: kernel)

`show <task_key>` reads task-local state with zero Git fan-out.
One attention queue lists unacknowledged deliveries, dirty closeouts, tripped breakers, blocked approvals, and failing checks.
Token and provider cost accrue per task.
Secrets and lease tokens never serialize into transcripts, status output, or logs.
Status, doctor, and direct database reads share one store and cannot disagree.

## Non-goals

No process ownership of external runs: a runner writes a terminal receipt; the kernel holds a wait predicate over it.
No semantic dedupe heuristics; typed lanes make them unnecessary.
No reserved manager slot; managers yield while children run.
No shared cross-repo code package; this document plus probed capabilities is the whole contract.
