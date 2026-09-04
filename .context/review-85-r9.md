# PR #85 — round 9 adversarial review

**Verdict: BLOCK — P1.** Reviewed exact head
`a746278c12a1a279578e860e9c0a82261a1150d0` against `origin/main`
`e73c1f0ca8989c37fd99cb08f3b8bd6ccbd5890b` on 2026-09-03.

## P1 — durable loss terminalizes a replacement outside the lifecycle fence

`src/sightmesh/durable.py:679-689` writes `lost` with
`wakes.finish_with_wake()` without `TaskStore.task_lock()`. This is a terminal
writer outside the fence used by initial launch, recovery, manual replacement,
and the SDK terminal API.

Concrete failure path:

1. The liveness scanner selects an `active` task and starts gathering evidence.
2. A manual `replace()` (or routing recovery) acquires the task lock, moves the
   row to `replacing`, and pauses in its native session launch.
3. The scanner's already-read active snapshot classifies the predecessor as
   `lost`; `_record_loss()` commits `lost` directly. `lost` legally follows
   `replacing` (`src/sightmesh/task_store.py:32`), so the write succeeds.
4. The native successor is then created, but `activate()` rejects the terminal
   row. The new cdesktop session is left running with no managed task owner.

The `DETECTABLE_STATES = ("active",)` guard is insufficient because selection
and the terminal write are not one fenced operation. The smallest robust fix
is to make the liveness terminal writers acquire the same per-task lifecycle
fence and reload the row under it before committing. In particular, perform
the loss evidence update and `finish_with_wake()` under that lock/transaction;
the stale pre-observation must be discarded when the row is now `replacing`.
Apply the same fence discipline to the approval-timeout terminal writer at
`src/sightmesh/durable.py:641` so terminal ownership has one invariant rather
than caller-specific exceptions.

## Fence audit

Launches converge correctly inside `src/sightmesh/sdk.py` when invoked through
the SDK: initial workspace launch enters `_start_reserved()` at 764-770;
recovery workspace launch enters `_advance_past_outcome()` at 531-535;
replacement workspace launch is called from that locked recovery path; and
teammate/session replacement is called from `replace()`'s lock at 316-346.
The direct SDK terminal writers (`complete`, `blocked`, `cancel`) converge via
`_finish_locked()` at 298-311. `cancel` records the terminal decision under
the lock, releases it before `stop_workspace()` at 383-384, then re-enters and
checks `current.version == updated.version` before `wakes.pump()` at 386-390.
Thus the new re-entry fence prevents a stale terminal actor from delivering a
wake after a newer row version, and neither stop nor wake HTTP is under that
terminal critical section. Managed launch HTTP deliberately remains inside the
lifecycle fence; that is necessary to serialize launch against cancellation.

The P1 path above means the claimed one-fence invariant is nevertheless not
global: `durable.py` has two direct terminal writers which bypass it.

## Evidence

- `git merge-base --is-ancestor origin/main a746278c12a1a279578e860e9c0a82261a1150d0` exited 0; merge base was `e73c1f0ca8989c37fd99cb08f3b8bd6ccbd5890b`.
- `uv run --with pytest pytest -q tests/simulator/test_routing_scenarios.py -k 'sd16'` → `6 passed, 26 deselected`.
- Mutation proof: locally removed the new initial-launch lock and restored the
  old stop-under-lock shape, then ran `uv run --with pytest --reinstall-package sightmesh pytest -q tests/simulator/test_routing_scenarios.py -k 'cancel_during_initial_launch or terminal_lock_is_released_before_stop_http'` → `2 failed, 30 deselected`. The failures were the initial activation rejected after cancellation and the lock-acquisition timeout. Restored exact head and reran the same command → `2 passed, 30 deselected`.
- `uv run --with pytest pytest -q tests/test_execution_routing.py tests/test_sdk.py` → `77 passed`.
- `uv run --with pytest pytest -q tests/simulator` → `62 passed`.
- `uv run --with pytest pytest -q` → `593 passed`.
- `gh-axi pr checks 85` on the exact PR head → `10 passed, 0 failed, 10 total` (Python 3.11/3.12/3.13 plus pinned-artifact and advisory source/package checks).

