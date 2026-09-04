# PR #106 round 2 review — BLOCK

Reviewed exact head `ba5bc25c504934657fb40109d935c198c7c3de90` against
`origin/main` on 2026-09-03.  `origin/main` is an ancestor of that head.

## P1 — concurrent first open can leave the external-run schema half-created

`src/sightmesh/escalation.py:443-458` treats any non-legacy table set as a
complete schema.  Concurrent fresh `EscalationStore` construction can therefore
interleave as follows: initializer A creates `external_run_leases`; initializer
B reads sqlite metadata after that statement but before A creates
`external_run_subscriptions`; B sees a nonempty, non-legacy set, returns from
`_migrate_external_run_schema`, and then attempts the pending index at
`src/sightmesh/escalation.py:367-370`. SQLite raises `no such table:
main.external_run_subscriptions`. This is not theoretical: the exact-head PR
workflow `33823721383` failed `test_concurrent_retirement_keeps_the_first_terminal_record`
with that error (537 passed, 1 failed, 1 skipped); the same-head push workflow
passed, making it an intermittent startup race.

Smallest robust fix: make schema creation converge for an absent or partial
current schema—use `CREATE TABLE IF NOT EXISTS` for both current external-run
tables (and the existing idempotent index), then call that convergence step when
the detected table set is incomplete. Preserve the rename/copy path only for a
complete legacy schema. That restores the invariant that every initializer sees
both tables before creating dependent indexes.

## Verification

- `origin/main` is an ancestor of `ba5bc25`.
- `uv run --with pytest pytest -q tests/test_external_runs.py tests/simulator/test_external_run_scenarios.py tests/test_leases.py`: **37 passed**. This covers S21-S24, re-acquisition after a released external root (with the retained receipt removed so the root is empty), parked recovery under the same dedupe key with one logical notification, stale bind fencing after restart, legacy live+released lease migration, and ordinary expired ownership-lease re-acquisition.
- The runner-disappears-without-receipt case reports exactly `lost/unknown`; it does not infer completion or failure.
- Negative control: locally removing `WHERE state = 'active'` from the live-root unique index made both parameterizations of `test_released_root_is_reclaimable_while_terminal_history_is_retained` fail with `UNIQUE constraint failed: external_run_leases.output_root`. The guard was restored; the restored targeted suite passed.
- The new external-run module only reads process start-time identity via `ps`; it contains no launch, stop, kill, restart, retry, or external-job outcome interpretation. Terminal state comes solely from the receipt, while missing receipt after a vanished fingerprint becomes `lost/unknown`.

## Verdict

**BLOCK.** The round-1 P1 lease and parked-delivery fixes, version fencing, S21-S24 coverage, and runner-death typing check out. However, the exact PR head has a P1 concurrent database-initialization regression and its pull-request CI is red, so it is not ready to merge.
