# cdesktop 0.2.6 durable recovery proof

Date: 2026-08-19

## Authority

- cdesktop worktree: `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/0bfc-cdesktop-026-dur/cdesktop`
- Branch: `cdt/0bfc-cdesktop-026-dur`
- Verified `HEAD`: `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`
- Verified `origin/main`: `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`
- SightMesh draft PR #16 read-only source: reviewed PR body and diff for the draft RC at `f827f425a024813e8f29d39e24420fbc81fe1838`.

No source code, GitHub state, tags, releases, running cdesktop service, or primary checkout state was modified.

## SightMesh 0.2.6 feature-detection dependency

SightMesh PR #16 gates durable recovery on cdesktop 0.2.6 support for native session command history, process-scoped requeue, explicit dispatch, and keyed stop replay. The relevant SightMesh-side assumptions observed in the PR diff are:

- `src/sightmesh/durable.py`: `DURABLE_RECOVERY_MIN_VERSION = (0, 2, 6)` and `supports_durable_recovery(version)` parse `/api/info` version text.
- `src/sightmesh/durable.py`: `DurableExecutionReconciler._supports_durable_recovery()` disables durable recovery after one bounded warning if cdesktop is older than 0.2.6.
- `src/sightmesh/cdesktop.py`: `requeue_execution_commands(session_id, execution_process_id)` calls `POST /api/sessions/{session_id}/commands/requeue`.
- `src/sightmesh/cdesktop.py`: `dispatch_queued(session_id)` calls `POST /api/sessions/{session_id}/commands/dispatch`.
- `src/sightmesh/durable.py`: keyed parent wake-ups use cdesktop follow-up dedupe keys derived from child command/terminal state, so repeated reconciliation observes the same native terminal row without creating another parent command.
- `src/sightmesh/durable.py`: stop retry semantics rely on cdesktop returning HTTP 425 for same-instance in-progress keyed stops, HTTP 409 for definitive rejection, HTTP 424 for interrupted/orphaned ownership, and success for accepted stop.

The PR also added `.context/cdesktop-durable-contract.md`, which states the same cdesktop 0.2.6 dependency and explicitly says cdesktop 0.2.5 does not expose the command history/requeue/dispatch boundary.

## Native cdesktop endpoints

The exact native cdesktop routes in this SHA are:

- `GET /api/sessions/{session_id}/commands`
  - Registered in `crates/server/src/routes/sessions/mod.rs:569`.
  - Returns durable `session_commands` rows for the session.
- `POST /api/sessions/{session_id}/commands/requeue`
  - Registered in `crates/server/src/routes/sessions/mod.rs:570`.
  - Body: `{ "execution_process_id": "<uuid>" }`.
  - Rejects a process from another session, rejects a still-running process, returns conflict when no interrupted command is available, and otherwise returns the number of rows requeued.
- `POST /api/sessions/{session_id}/commands/dispatch`
  - Registered in `crates/server/src/routes/sessions/mod.rs:571`.
  - Dispatches currently pending commands for the session.
- `POST /api/execution-processes/{id}/stop`
  - Registered by `crates/server/src/routes/execution_processes.rs:491-512`.
  - Legacy mode: absent `dedupe_key` preserves the existing unkeyed stop behavior.
  - Recovery mode: body may include `{ "dedupe_key": "<caller-owned-key>" }`.

## Native invariants

- `session_commands` are keyed per `(session_id, dedupe_key)` when `dedupe_key` is present; duplicate enqueue returns the existing row rather than appending another command (`crates/db/src/models/session_command.rs:68-100`).
- Pending commands are claimed in row order by assigning `execution_process_id` and moving `state` from `pending` to `claimed` (`crates/db/src/models/session_command.rs:144-181`).
- Interrupted claimed commands can be released back to `pending` without changing their row identity (`crates/db/src/models/session_command.rs:193-205`).
- Process-scoped requeue returns terminal/interrupted command rows to `pending` and clears `execution_process_id`/`finished_at` while preserving `id` and `dedupe_key` (`crates/db/src/models/session_command.rs:207-235`).
- Killed-process requeue may include `done` rows because keyed stop can race the exit monitor; the route only uses that wider transition after loading a killed execution process (`crates/server/src/routes/sessions/mod.rs:421-437`).
- Keyed stop creates a durable `execution_process_stop_operations` row before attempting the stop side effect; completed outcomes are never overwritten (`crates/db/src/models/execution_process_stop_operation.rs:48-130`).
- Keyed stop outcome is scoped to `(execution_process_id, dedupe_key)`, with `accepted`, `rejected`, and `interrupted` as distinct durable outcomes (`crates/db/src/models/execution_process_stop_operation.rs:4-43`).
- Same-instance pending keyed stop returns HTTP 425/Too Early and asks the caller to retry the same key; different-instance pending keyed stop completes as `interrupted` and returns HTTP 424/StopInterrupted rather than re-executing the stop (`crates/server/src/routes/execution_processes.rs:336-367`, `396-408`).
- `accepted` replays as success, `rejected` replays as HTTP 409/Conflict, and `interrupted` replays as HTTP 424/StopInterrupted (`crates/server/src/routes/execution_processes.rs:340-408`).

## Commands and outcomes

### Baseline

```sh
pwd && git rev-parse HEAD && git rev-parse origin/main && git status --short --branch
```

Outcome: `HEAD` and `origin/main` were both `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`; branch was `cdt/0bfc-cdesktop-026-dur`; no dirty files were reported.

### Fleet awareness

```sh
sightmesh peers
sightmesh peek @release-candidate-integration
```

Outcome: confirmed this agent is `@cdesktop-026-durable-proof`; observed the separate SightMesh integration workspace without modifying it.

### Read-only SightMesh PR source

```sh
~/.local/bin/gh-axi pr view 16 --repo clarkipeng/sightmesh --full --comments
~/.local/bin/gh-axi pr diff 16 --repo clarkipeng/sightmesh --full | perl -0pe 's/\\n/\n/g; s/\\"/"/g' | rg -n -C 6 "class DurableExecutionReconciler|def supports_durable_recovery|commands/requeue|commands/dispatch|dedupe_key|stop_execution|425|409|424|feature|cdesktop 0\\.2\\.6"
~/.local/bin/gh-axi pr diff 16 --repo clarkipeng/sightmesh --full | perl -0pe 's/\\n/\n/g; s/\\"/"/g' | awk '/^diff --git a\/src\/sightmesh\//{p=1} /^diff --git / && $3 !~ /^a\/src\/sightmesh\//{p=0} p' | rg -n "^diff --git|^@@|class |def |commands/requeue|commands/dispatch|dedupe_key|durable|reconcile|requeue|dispatch|stop_execution|info"
```

Outcome: confirmed PR #16 is open/draft and contains the cdesktop 0.2.6 durable command contract. The diff identified SightMesh's feature gate and native endpoint calls in `src/sightmesh/durable.py` and `src/sightmesh/cdesktop.py`.

Exact reviewed commit check:

```sh
tmp=$(mktemp -d /tmp/sightmesh-pr16-XXXXXX)
git -C "$tmp" init -q
git -C "$tmp" remote add origin https://github.com/clarkipeng/sightmesh.git
git -C "$tmp" fetch --depth=1 origin f827f425a024813e8f29d39e24420fbc81fe1838
git -C "$tmp" show --stat --oneline FETCH_HEAD
git -C "$tmp" show FETCH_HEAD:.context/cdesktop-durable-contract.md | sed -n '1,80p'
git -C "$tmp" show FETCH_HEAD:src/sightmesh/durable.py | rg -n "DURABLE_RECOVERY_MIN_VERSION|supports_durable_recovery|_supports_durable_recovery|reconcile_sessions|stop_execution|CdesktopInterruptedError|425|409|notify_parent|requeue"
git -C "$tmp" show FETCH_HEAD:src/sightmesh/cdesktop.py | rg -n "session_commands|requeue_execution_commands|commands/requeue|commands/dispatch|stop_execution|dedupe_key"
```

Outcome: fetched exact commit `f827f42 fix: redact migration lease reports` into `/tmp` and confirmed `.context/cdesktop-durable-contract.md`, `src/sightmesh/durable.py`, and `src/sightmesh/cdesktop.py` contain the durable recovery contract, feature gate, requeue/dispatch calls, and keyed stop usage described above.

### Keyed stop operation proof

```sh
cargo test -p db execution_process_stop_operation -- --nocapture
```

Outcome: passed after first-build compilation.

Evidence: `7 passed; 0 failed; 0 ignored; 42 filtered out`.

Covered:

- `accepted_stop_replays_after_a_lost_response_without_another_stop`
- `completed_stop_replays_after_server_restart`
- `rejected_stop_replays_as_the_same_definitive_outcome`
- `interrupted_stop_replays_as_a_distinct_durable_outcome`
- `dedupe_key_is_scoped_to_an_execution_process`
- `concurrent_pending_follower_replays_the_owner_outcome_without_stopping`
- `owner_crash_before_stop_side_effect_replays_interrupted_without_takeover`

This proves completed requests are replay-safe, pending followers do not issue another stop, rejected/interrupted terminal cases remain distinct, and owner crash after intent but before side effect recovers as a bounded interrupted result.

### Session command recovery proof

```sh
cargo test -p db session_command -- --nocapture
```

Outcome: passed after waiting behind Cargo artifact locks from the parallel focused runs.

Evidence: `7 passed; 0 failed; 0 ignored; 42 filtered out`.

Covered:

- `enqueue_is_append_only_and_idempotent_when_keyed`
- `claim_batches_pending_commands_in_order`
- `claim_stops_before_a_config_change`
- `interrupted_execution_returns_to_pending`
- `terminal_failed_execution_requeues_with_its_original_dedupe_key`
- `requeue_is_process_scoped_and_duplicate_safe_after_reopen`
- `replace_cancels_only_older_pending_commands`

This proves pending/interrupted commands recover once to the same durable row, requeue preserves dedupe identity, and duplicate reopen/requeue has a bounded observable outcome of `0` rows on the second call.

### Route-level response mapping proof

```sh
cargo test -p server routes::execution_processes::tests -- --nocapture
```

Outcome: passed after first-build compilation. The server build script created ignored dummy frontend build output in `packages/local-web/dist` for compilation only; no running service was touched.

Evidence: `4 passed; 0 failed; 0 ignored; 44 filtered out`; `generate_types` and `main` bins ran zero matching tests.

Covered:

- `orphaned_intent_never_infers_acceptance_from_natural_exit_status`
- `keyed_stop_outcomes_keep_rejection_and_interruption_distinct`
- `omitted_dedupe_key_preserves_the_legacy_stop_request`
- `normalized_snapshot_coalesces_streaming_replacements`

This proves terminal/orphaned keyed stop cases do not infer acceptance from unrelated natural process exit, and the route maps rejected/interrupted outcomes to distinct protocol responses instead of causing an endless wake loop.

## Required behavior matrix

| Requirement | Evidence |
| --- | --- |
| Completed requests are replay-safe | `accepted_stop_replays_after_a_lost_response_without_another_stop`, `completed_stop_replays_after_server_restart`; completed `execution_process_stop_operations` rows replay original outcome without another stop. |
| Pending or interrupted requests recover once | `concurrent_pending_follower_replays_the_owner_outcome_without_stopping`, `owner_crash_before_stop_side_effect_replays_interrupted_without_takeover`, `interrupted_execution_returns_to_pending`. |
| Unsupported or terminal cases do not create endless wake loop | SightMesh feature gate disables durable recovery below 0.2.6 after one bounded diagnostic; cdesktop route maps rejected to 409 and interrupted to 424; orphaned terminal status never infers acceptance. |
| Retry has bounded observable outcome | Same key while owner is in progress yields 425; accepted/rejected/interrupted replay to stable final responses; second process-scoped command requeue returns `0` rows and the route reports conflict when no interrupted command is available. |

## Gaps and limits

- I did not run a live cdesktop backend, create workspaces, or call the user's running service. The proof uses the narrowest existing Rust unit/integration harnesses with in-memory or disposable SQLite state.
- I did not run broad workspace checks (`pnpm run check`, `cargo test --workspace`, or `pnpm run format`) because the task excludes unrelated broad runs and source changes; only this proof document was added.
- I did not independently execute the SightMesh Python tests at PR #16; I used PR #16 read-only diff/body to identify feature-detection assumptions, then proved the native cdesktop side those assumptions rely on.
- The native cdesktop API still has no expiry fact or general command metadata mutation; SightMesh's PR documents `expired` as unimplemented until cdesktop adds such a native state.

## Gate decision

Sufficient for the requested cdesktop 0.2.6 durable-recovery release gate from the cdesktop side.

The native endpoints and invariants SightMesh relies on exist at `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`, and the focused disposable harnesses prove replay-safe keyed stop outcomes, one-time command recovery/requeue, bounded retry results, and terminal/interrupted cases that do not imply endless wake loops. No concrete cdesktop defect was found.
