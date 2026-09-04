# PR #85 independent review — round 7

Reviewed `cp/routing-wiring` at exact head `2bedeb7369487a31b9bb2aab384cd14dc61d8595` against `main` at `86b5d99ed396681b1886057bbc9e43c9089c4c1f` (`5f07e61` contains the prior runtime-script repair). Scope included the typed-outcome failover seam, standard/deep class selection, recovery launch fencing/serialization, unattended-manager classification, and their tests. PR code was not modified.

## Verdict: BLOCK

The round-6 runtime-script P1 is closed, and the four head commits otherwise have focused and full-suite coverage. However, recovery serialization remains incomplete: a terminal lifecycle operation can race a recovery launch and leave an active native successor with no live managed task. This is a P1 because cancellation/completion is expected to stop the work, yet the created successor is orphaned and runs unmanaged.

### P1 — terminal lifecycle calls bypass the recovery intent lock

- Location: `src/sightmesh/sdk.py:293-307` (especially `cancel` at `303-307`); recovery holds the lock at `src/sightmesh/sdk.py:496-500` through its native launch at `576-577` / `602`.
- Failure path: an automatic recovery prepares epoch N with `target.recovery`; its reconciler acquires `TaskStore.task_lock` and pauses in `client.managed_launch`. Concurrent `cancel()` does **not** acquire that lock, stops the old workspace, and transitions the row to `cancelled`. Recovery resumes, cdesktop creates epoch N's successor, then `TaskStore.activate()` rejects the now-terminal task as stale. The task stays `cancelled`, but the native effect stays `active`, so no managed row owns or will clean up that session. `complete()` and `blocked()` have the same unlocked terminal-transition shape.
- Reproduction: a standalone adversarial harness using the repository's real `TaskStore`, `SightMesh`, and simulator `FakeCdesktop` printed:

  ```text
  cancelled cancelled
  recovery_error StaleTransition Task 'audit' is cancelled at version 3; it cannot transition to active
  task_state cancelled
  native_effect {'state': 'active', 'workspace_id': 'workspace-…', 'session_id': 'session-…-2'}
  ```

- Smallest robust fix: make the per-task intent-lock invariant cover terminal lifecycle writers as well as recovery/manual replacement. Have public `complete`, `blocked`, and `cancel` acquire `task_lock`, reload the task inside it, then perform the terminal operation (and, for cancellation, stop the reloaded current workspace) while the lock remains held. Keep `_finish` unlocked because recovery's in-lock failure paths call it; add simulator race tests for cancellation and the other terminal writers at the native-launch boundary.

## Evidence

| Command | Outcome |
| --- | --- |
| Bare venv with `PATH=<bare>/bin:$PATH scripts/check-cdesktop-runtime.sh` | Exit 1; `ModuleNotFoundError: No module named 'sightmesh'`. The import failure is no longer masked. |
| Installed venv (`pip install .`) with the same script | Exit 0; `runtime-compatibility: verified 0.2.7 package checksum and executable`. |
| `uv run --with pytest pytest -q tests/test_execution_routing.py tests/test_sdk.py tests/test_cdesktop.py tests/test_pool.py tests/simulator/test_routing_scenarios.py` | `180 passed in 3.49s`. |
| `uv run --with pytest pytest -q` | `494 passed in 14.32s`. |
| Standalone cancel-versus-paused-recovery simulator harness | Reproduced the P1 orphan above. |

The P1 must be fixed and regression-tested before approval.
