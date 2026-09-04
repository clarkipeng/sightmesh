# PR #85 round 8 — BLOCK

Reviewed `cp/routing-wiring` at `5e822de21718ba664c945a305c3bac4c492494af` against freshly fetched `origin/main` (`5f07e610517c880c64328b7de53b2e41c5d98248`). The head equals `origin/cp/routing-wiring` and `origin/main` is an ancestor, so it is rebased. The four round-7 routing commits (`19435f1`, `92f1c22`, `720523c`, `2bedeb7`) still preserve their intended recovery-marker clearing, stale-recovery fencing, task-scoped recovery serialization, and fan-out class selection behavior. No regression found in those changes.

## P1 — initial workspace launches still bypass the terminal/launch gate

`src/sightmesh/sdk.py:207-210` calls `_start_reserved()` without `TaskStore.task_lock`; `_start_reserved()` reaches the native `managed_launch()` at `src/sightmesh/sdk.py:752-763` / `821`. `complete`, `blocked`, and `cancel` now lock in `_finish_locked()` (`src/sightmesh/sdk.py:293-307`, `373-379`), but that cannot serialize with this launch because the launcher does not take the same lock.

Failure path: a task is reserved and its initial `managed_launch()` is paused in cdesktop; a concurrent `cancel(key)` acquires the otherwise-unused task lock, sees no workspace yet, and writes `cancelled`; the native launch then succeeds and `_start_reserved()` calls `activate()`, which raises `StaleTransition` because the row is terminal. The new cdesktop workspace/session remains running while its task is cancelled: the same orphan invariant round 7 blocked, just through the initial workspace-launch path. The new S-D16 test only pauses `_advance_past_outcome()` recovery and therefore does not cover this writer/launcher pairing.

Smallest robust fix: give every native launch and every terminal transition one durable launch/cancellation fence. In particular, do not make the file lock the lifetime of an HTTP request: persist a launch/cancel intent atomically, have the launch owner re-check that fence after the native response and stop/retire a response that lost to cancellation, and let terminalization happen independently. Add the corresponding paused-initial-launch-vs-cancel simulator regression.

## Lock audit

No circular task-lock acquisition is present: each shown path takes one task-keyed flock. However, the stated no-network-under-lock condition is not met. `_finish_locked()` holds the flock while `stop_workspace()` (`src/sightmesh/sdk.py:373-379`) and then `_finish()` → `wakes.pump()` (`src/sightmesh/sdk.py:343-355`) execute. Those invoke HTTP through `CdesktopClient.request()` (`src/sightmesh/cdesktop.py:273-315`); wake delivery sends at `src/sightmesh/wakes.py:223-229`. Each request has a 15-second timeout, so this is bounded rather than a proven permanent deadlock, but it serializes unrelated terminal/recovery work for the same task across cdesktop latency and is incompatible with the required lock boundary.

Terminal-writer enumeration: public `complete`, `blocked`, and `cancel` route through `_finish_locked`; routing’s `_block_unroutable` (`src/sightmesh/sdk.py:627-638`), definitive-rejection handling (`829-834`), and lost-launch handling (`843-847`) call `_finish` directly. The latter are lock-covered only when reached from `replace`/recovery; the initial `_start_reserved` path is not covered, which creates the P1 above.

## Verification

All commands run at `5e822de` after restoring the temporary reversal:

* `git revert --no-commit 5e822de && git restore --source=HEAD -- tests/simulator/test_routing_scenarios.py && uv run --with pytest pytest -q tests/simulator/test_routing_scenarios.py::test_sd16_cancel_waits_for_recovery_then_stops_the_successor` — **failed as required**: recovery later attempted `activate` after the task became `cancelled` (`StaleTransition`). `git revert --abort` restored the exact head and a clean worktree.
* `uv run --with pytest pytest -q tests/simulator/test_routing_scenarios.py::test_sd16_cancel_waits_for_recovery_then_stops_the_successor tests/simulator/test_routing_scenarios.py::test_sd16_one_tasks_native_launch_does_not_serialize_another_task` — **2 passed**.
* `uv run --with pytest pytest -q tests/test_execution_routing.py tests/test_sdk.py tests/test_effects.py tests/simulator/test_routing_scenarios.py` — **127 passed**.
* `uv run --with pytest pytest -q tests/simulator` — **54 passed**.
* `uv run --with pytest pytest -q` — **495 passed**.

`gh-axi pr checks 85` reports the exact PR head with 9 passing, 0 failing, and 1 pending lane: `pinned cdesktop artifact`; no red CI lane is present. This pending lane prevents calling CI fully green.

Verdict: **BLOCK** for the remaining P1 orphan race on initial workspace launch. The submitted recovery-cancel test is meaningful and the recovery path it tests is fixed, but the invariant has not been extended to all native launch paths and the terminal lock now spans cdesktop I/O.
