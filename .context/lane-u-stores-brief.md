# Lane U brief: consolidate durable state stores (issue #27)

Single writer. Read clarkipeng/sightmesh#27 first. Net-negative preferred.

## Base
sightmesh `main` (must contain PR #40 and #41 squashes). Verify before writing.

## Scope
- Succession ownership lives in a JSON file (`succession.py` OwnershipStore); escalations/acks/order expectations/signal policies live in the escalation SQLite store. One class of state, two durability mechanisms.
- Migrate ownership records into the SQLite store: same atomicity guarantees (first-write-wins retirement), one-time transparent migration from an existing ownership.json on first open (read, import, rename to ownership.json.migrated), identical public API on OwnershipStore so consumers (durable.py, bridge.py, cli.py) do not change behavior.
- Delete the JSON persistence code once migrated. Owned paths: `src/sightmesh/succession.py`, `src/sightmesh/escalation.py` (schema/table addition only), matching tests.

## Proof
Why-docstringed tests: first-write-wins survives concurrent retire attempts; migration imports existing JSON exactly once and is idempotent; quarantine checks read identically post-migration. Full suite green. Report net lines.

## Delivery (lane policy C)
Draft PR then self-ready on green gates, explicit --repo clarkipeng/sightmesh, base main. STATUS to `/Users/clarkpeng/Documents/Code/sightmesh/.context/lane-u-status.md` (absolute path, not your worktree) AND `sightmesh parent --message`. Reviewer merges. No background processes; never message retired sessions.
