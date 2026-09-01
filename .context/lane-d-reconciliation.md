# Lane D reconciliation: durable succession quarantine and routed auto-launch

Date: 2026-08-18 America/Los_Angeles.
Reconciled by: hot-swap train manager 5 (session dd76).

## Assignment state

- Objective: SightMesh autolaunch/reconciler - routed spawn/teammate launches, quota cooldown and durable requeue, restart recovery, cross-executor successor linkage, explicit-retirement quarantine.
- Owner: @lane-d-reconciler, session `c34df815-cb59-44a9-bb54-69e1c7648c07`, workspace `17e13e26`, status completed.
- Repo: sightmesh, checkout `.cdesktop-workspaces/17e1-lane-d-reconcile/sightmesh`.
- Branch: `cdt/17e1-lane-d-reconcile`, HEAD exactly `fdf12e0c6552d2dafd54b4e3893f6dd6a70b3ea2`, pushed (local == remote, verified), based on C2 `be40617b` (ancestry verified).
- PR: clarkipeng/sightmesh #23, open draft, base `cdt/1ebb-lane-c2-routing`, head `fdf12e0c` (verified).
- Dirty/untracked/unpushed: none.
- Checks: worker reported `env -u CDESKTOP_SESSION_ID uv run --with pytest pytest -q` 217 passed 0 failed. Manager independently re-ran the same full suite in D's worktree: 217 passed 0 failed.
- No merges taken: Lane K's `escalation.py` untouched (not needed in code), selector internals untouched.
- Classification: delivered.

## Delivered invariants (for the release gate and final rereview)

- Durable terminal-ownership store; quarantine by construction for explicitly retired/superseded sessions only: retirement atomically records terminal state, cancels pending commands, rejects later message/steer/prompt-idle and peer-bridge delivery, all before a successor launches.
- Completed turns remain resumable (proven both ways by 12 new succession tests, including retired-cannot-resume and completed-turn-still-resumes).
- Reconciler cancel-never-requeue for quarantined sessions; durable quota cooldown with next-route selection; routed spawn/teammate launches carry opaque `auth_binding_id`; failover ownership transfer preserves exactly one logical command across executors.

## Ownership transition

- Lane D scope is closed. Retired D session must never be messaged, steered, or prompted.
- Archive decision: deferred to final closeout.
