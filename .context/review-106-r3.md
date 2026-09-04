# PR #106 review, round 3 — `b4df6e1`

**Verdict: BLOCK.** The round-2 fresh-schema race is fixed in the reviewed
commit, and the external-run correctness findings from rounds 1 and 2 remain
closed. However, `origin/main` is now `eee10a4` (including #83) and is not an
ancestor of the exact head. A merge simulation has a real `escalation.py`
conflict and delete-vs-modify reconciliation for the external-run feature, so
this head cannot be safely merged as reviewed.

## Findings

### P1 — rebase and reconcile the #83 replacement of the affected surfaces

- **Location:** `src/sightmesh/escalation.py:294`, `src/sightmesh/durable.py:328`,
  `src/sightmesh/cli/tasks.py:114`, `src/sightmesh/external_runs.py:1`
- **Failure path:** `origin/main` at `eee10a4` contains #83, which rewrites
  `escalation.py` and deletes `cli/tasks.py`, `external_runs.py`,
  `tests/test_external_runs.py`, and the external-run simulator scenarios.
  `git merge-tree e73c1f0 origin/main b4df6e1` reports a changed-in-both
  conflict for `escalation.py` and added-in-remote (delete-vs-modify) entries
  for the external-run source and tests. Merging or mechanically rebasing this
  head could discard #83's observability architecture or reinstate the deleted
  command/module shape without integrating it.
- **Smallest robust fix:** rebase onto current `origin/main`, retain #83's
  ownership/observability model, then deliberately re-home the durable
  subscription, reconciliation tick, CLI verbs, and tests in that model. Run
  the complete suite and CI again from the rebased commit.

### P2 — the new concurrency test does not prove serialization by mutation

- **Location:** `tests/test_external_runs.py:183`
- **Failure path:** removing only `BEGIN IMMEDIATE` and `COMMIT` from
  `EscalationStore._initialize` (`src/sightmesh/escalation.py:302,395`) left
  this 12-way test green in 20 consecutive local runs. The new convergent DDL
  helper also creates both tables before either dependent index, so it masks
  the former interleaving even without the write lock. Thus the requested
  mutation check cannot demonstrate that removing serialization regresses the
  contract.
- **Smallest robust fix:** make the regression assert the atomic-initialization
  contract with deterministic synchronization/instrumentation, or explicitly
  narrow its claim to convergent DDL. Keep the write transaction as the
  production invariant either way.

## Verified fixes and evidence

- Initialization is one write-locked transaction: `_initialize` begins
  `BEGIN IMMEDIATE` before schema inspection/migration and commits after all
  tables, indexes, and `user_version` (`src/sightmesh/escalation.py:294-395`).
  `_external_run_schema` creates `external_run_leases`, then
  `external_run_subscriptions`, then their dependent indexes
  (`src/sightmesh/escalation.py:402-449`). `IF NOT EXISTS` makes the current
  schema convergent; S24 opens the legacy previous schema, reopens it, retains
  release history, and reclaims only the released root
  (`tests/simulator/test_external_run_scenarios.py:146-196`).
- The requested 12-way fresh-store test passed on CPython 3.13.12. With the
  exact head restored, focused external-run/lease tests passed **31**, all
  simulator tests passed **30**, and the complete suite passed **540**.
- The exact-head GitHub checks are green: both compatibility workflow runs
  report `test (3.11/3.12/3.13)` passing, plus both runtime checks (10/10).
  The workflow runs unfiltered `pytest` on 3.13
  (`.github/workflows/compatibility.yml:38-57`), so the repaired lane is not
  skipped.
- Round-1/2 behavior remains covered and implemented: terminal notification
  releases only the live lease, allowing released-root re-acquisition
  (`tests/test_external_runs.py:227-253`); parked recovery retains one durable
  dedupe key and delivers once (`tests/test_external_runs.py:256-272`);
  stale versions are fenced (`tests/test_external_runs.py:275-294`); and a
  vanished/reused fingerprint records `lost/unknown`
  (`src/sightmesh/external_runs.py:390-405`, simulator S19).

