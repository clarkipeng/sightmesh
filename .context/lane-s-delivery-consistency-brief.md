# Lane S brief: quarantine-safe terminal wakes + delete dead recovery store (Lane R findings 3 and 6)

Single writer. Net-negative preferred. Read `.context/lane-r-findings.md` items 3 and 6 first.

## Base
sightmesh `main` (must contain PR #35 and #36 squashes). Verify before writing.

## Scope
1. Finding 3: `_wake_parent_for_terminal_commands` in `src/sightmesh/durable.py` sends directly to `parent` while the sibling child-terminal path resolves a live successor first. Route BOTH through the same successor/quarantine resolution (`resolve_live_successor`, park-or-drop semantics identical to `reconcile_child_terminal`). One shared helper, not two copies.
2. Finding 6: `RecoveryIntentStore` in `src/sightmesh/stalls.py` (~100 lines) plus its `# noqa: F401` compatibility import in `src/sightmesh/bridge.py` is dead machinery contradicting the convergence contract. Delete the store, the import, and its tests; keep only what live stall detection actually uses.
3. Owned paths: `src/sightmesh/durable.py`, `src/sightmesh/stalls.py`, `src/sightmesh/bridge.py`, matching tests.

## Proof
Why-docstringed tests: a retired parent never receives a terminal-command wake (delivered to successor or parked, exactly once); grep proves no `RecoveryIntentStore` references remain. Full suite green (`env -u CDESKTOP_SESSION_ID uv run --with pytest --with build pytest -q`). Report net line count.

## Delivery (lane policy C)
Draft PR then self-mark ready on green local gates, explicit `--repo clarkipeng/sightmesh`, base main. Append STATUS (branch, exact head, PR, tests, net lines) to `.context/lane-s-status.md` AND `sightmesh parent --message`. Reviewer merges. No background processes; never message retired sessions.
