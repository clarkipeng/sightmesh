# PR #85 review — round 10

Reviewed exact head `be15730c0e6a27edccc090861927fe7ae94d8cb4` against
`origin/main` on 2026-09-03. `origin/main` is an ancestor of that head.

## Verdict: BLOCK

### P1 — Task fence is held across cdesktop HTTP

- **Locations:** `src/sightmesh/sdk.py:817-821`, `src/sightmesh/sdk.py:828-830`,
  `src/sightmesh/sdk.py:894`; `src/sightmesh/sdk.py:566-570` reaches the same
  launch path; `src/sightmesh/effects.py:279-288`.
- **Failure path:** initial launch and routed recovery acquire the per-task
  `flock`, then call `CdesktopClient.managed_launch()` before releasing it.
  Expired-effect adoption likewise retains the fence while asking
  `managed_effect()`. `CdesktopClient.request()` uses a 15-second HTTP timeout
  (`src/sightmesh/cdesktop.py:295`). A slow/unreachable cdesktop therefore
  prevents cancel, completion, liveness-loss recording, and replacement of that
  task for the whole request. This is explicitly contrary to the requested
  no-lock-across-cdesktop-HTTP invariant. The added
  `test_sd16_terminal_lock_is_released_before_stop_http` proves only the later
  `stop_workspace` call is outside the fence; it does not cover either launch
  or adoption lookup.
- **Smallest robust fix:** make the durable effect row/epoch CAS the external-I/O
  claim. Under the task fence, reload and atomically reserve/claim the epoch;
  release it for the cdesktop request; reacquire, reload, and conditionally
  adopt/activate only the same claimed epoch. Recovery then reconciles an
  uncompleted claimed effect. This preserves one native launch per epoch without
  turning an unavailable cdesktop endpoint into a lifecycle mutex.

## Closed class checks

The round-9 stale liveness-loss terminalization is closed: `_record_loss` takes
the task fence, reloads beneath it, requires the observed epoch and `active`
state, and calls `finish_with_wake` with that capability
(`src/sightmesh/durable.py:683-703`). Temporarily replacing that fence with a
no-op made
`test_sd16_liveness_loss_wins_over_a_paused_replacement_without_orphaning`
fail: recovery launched an active epoch-2 successor rather than returning
`None`. Restored before this report.

Lifecycle/epoch writers were enumerated from every `managed_tasks` mutation in
`src/`. Initial `reserve_all` insertion is serialized by its unique row and
`BEGIN IMMEDIATE`; the state/epoch transition funnel is
`TaskStore.transition` (`src/sightmesh/task_store.py:698-772`), which rejects
any missing/wrong `TaskFence`. Activation, replacement, terminalization, manual
replacement resume, liveness loss, and expired-effect adoption all pass a fence.
The remaining direct mutations are migrations and non-lifecycle metadata
(checkpoint/liveness/wake watermarks). A temporary scratch test with a direct
unfenced `transition()` was rejected with `TaskStoreError`. Temporarily deleting
`fence=` from `effects.py:302` made the AST structural guard fail with exactly
that offender. Both probes were reverted.

The P1 does not invalidate the typed-outcome route selection, standard/deep
classes, recovery fencing, or unattended-manager classification: the complete
suite passed and the S-D16 scenarios passed at the reviewed head before fault
injection.

## Verification

- `uv run --with pytest pytest -q` — **596 passed**.
- Targeted lifecycle, routing, S-D16, effects, SDK, and pool tests — passed.
- Fault injection: liveness-loss fence removal made the corresponding S-D16
  race fail; restored.
- Fault injection: an unfenced lifecycle call was rejected; deleting a caller's
  `fence=` caused `test_every_transition_caller_passes_the_fence_capability` to
  fail at `sightmesh/effects.py:302`; restored.
- PR #85 checks for exact head: **10 passed, 0 failed** (three Python test
  versions, package/runtime checks, and advisory edge checks).
- `uvx ruff check` on the lifecycle/routing files above and their direct tests
  is clean. A repository-wide current Ruff invocation reports 90 pre-existing
  style/static diagnostics; CI does not run Ruff, so the unqualified “ruff
  clean” claim is not independently established for the entire repository.
