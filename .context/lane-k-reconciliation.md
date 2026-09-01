# Lane K reconciliation: Conductor-safe durable parent escalation

Date: 2026-08-18 America/Los_Angeles.
Reconciled by: hot-swap train manager 5 (session dd76).

## Assignment state

- Objective: capture external launcher identity, durable parent fallback, durable decision inbox when no cdesktop parent exists; never deliver into retired/archived sessions.
- Owner: @lane-k-parent-escalation, session `9e493a15-f206-4a2c-bb9f-7814fbdd37be`, workspace `fb85ccda-efa9-467b-8917-bd0dde4d92a1`, status complete per worker report.
- Repo: sightmesh, checkout `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/fb85-lane-k-parent-es/sightmesh`.
- Branch: `cdt/fb85-lane-k-parent-es`, HEAD exactly `fa2defe148e06e3e2f6ba4df45dc3b5b7b973f0d`, pushed (local == remote, verified). Base main `5622486f`.
- PR: clarkipeng/sightmesh #22, draft, base `main`, head `fa2defe1`, CI 5 passed 0 failed (1 pending at verification time).
- Dirty/untracked/unpushed: none (`git status --porcelain` empty, verified by manager).
- Checks: worker reports 199 passed 0 failed full suite via `env -u CDESKTOP_SESSION_ID uv run pytest -q`. Manager independently re-ran the focused suites `tests/test_escalation.py tests/test_cli.py`: 55 passed 0 failed.
- Known environment caveat: 4 pre-existing spawn tests fail when `CDESKTOP_SESSION_ID` leaks from the surrounding cdesktop session; worker verified pre-existing on main, unrelated to this change. Carry to release gate notes.
- Classification: delivered. No blocked or missing scope.

## Delivered contract for consumers (Lane D and release gate)

- `src/sightmesh/escalation.py`: durable SQLite `EscalationStore` + `escalate()`; delivers only to a confirmed live, non-archived parent, otherwise durably parks in a decision inbox; never drops, never delivers into archived/retired sessions.
- Launcher identity (`cdesktop` vs `external`, Conductor hint) captured durably at spawn for every session.
- `cmd_parent` and `_spawn_workspace` are wired through the store; new `sightmesh escalations` inbox listing command.

## Ownership transition

- `escalation.py` remains K-owned surface; Lane D consumes its API and must not rewrite it. D owns reconciler integration, cooldown/requeue, restart recovery, successor linkage, and quarantine of superseded sessions.
- Retired K session must never be messaged, steered, or prompted.
- Archive decision: deferred to final closeout; keep branch, transcript, and this handoff.
