# Kernel v1 review fixes (round 1)

Seven defects from independent review of PR #80. Fix all on `cp/kernel-v1`; each gets a locking simulator scenario (S18-S24) that fails before the fix and passes after.
The unifying theme: a terminal effect and a consumed wake must both be re-armable or barrier-correct, and every guarded readback must be in its write's transaction.

## F1 - wakes never re-arm (CRITICAL, spec defect)

`dedupe_key = {parent}:{parent_epoch}:{predicate}` plus `INSERT OR IGNORE` on a lifetime-UNIQUE index means a manager wakes once per predicate per epoch, ever. Multi-wave managers hang after wave 1.

Fix: uniqueness must bind only *live* wakes, not historical ones.
Replace the column `UNIQUE` on `task_wakes.dedupe_key` with a partial unique index:

```sql
CREATE UNIQUE INDEX idx_task_wakes_live
ON task_wakes(dedupe_key) WHERE state IN ('pending', 'claimed');
```

`INSERT OR IGNORE` then collapses only concurrent duplicate signals for the same un-consumed cohort event; once a wake is `delivered`/`resolved`, the same key can arm again for the next cohort transition.
Amend `kernel-spec.md:55` and `kernel-contract.md` to state re-arm semantics explicitly.
S18: parent with children a1,a2 both terminal -> one wake, pump, deliver; dispatch a3,a4, both terminal -> a *second* wake delivers. (Fails today: second `record_wakes` returns [], pump 0.)

## F2 - terminal effect is not a launch barrier (CRITICAL)

`reserve()` folds `launched` and `terminal` into `(existing, False)`; `_journaled_launch` short-circuits only on `launched`, so a terminal effect relaunches, and `_advance`/`mark_launched` silently no-op (no rowcount check).

Fix:
- `reserve()` distinguishes states explicitly: `reserved`+live+same-owner -> adopt; `reserved`+expired -> fenced takeover; `reserved`+live+other-owner -> `EffectBusy`; `launched` -> adopt; `terminal` -> raise new `EffectTerminal` (this epoch is finished; the caller must advance the epoch, never relaunch it).
- `_advance` checks `cursor.rowcount == 1` and raises `TaskStoreError` otherwise - no silent write loss.
S19: mark an effect terminal, then a second `reserve()` on the same `(task, epoch)` raises `EffectTerminal`; `mark_launched` on a terminal row raises, not no-op.

## F3 - expiry buries an in-flight launch (CRITICAL)

`expire_reservations` marks any reserved effect past its lease `lost:reservation-expired` using only `session_id IS NULL`, with no native lookup. The 15s client timeout makes the "session created but mark_launched never ran" window ordinary, so a live session gets orphaned.

Fix: expiry must adopt-or-lose. For each expired reserved effect, call `client.managed_effect(task_id, epoch)`:
- native reports active with ids -> `mark_launched` + `store.activate` (adopt);
- native genuinely absent/lost -> `mark_terminal("lost:reservation-expired")`.
The reconciler already holds a client; thread it into `expire_reservations`.
S20: reserve, simulate native session created but not marked, advance clock past lease, run expiry -> effect ends `launched` (adopted), task active, no `lost`.

## F4 - EffectConflict outranks terminal, dead-ending an epoch (CONFIRMED path)

`reserve()` checks `request_hash` before `state`, so a terminal/launched row with a different hash raises `EffectConflict` that nothing can clear (expiry sets terminal but reserve raises first; replace can't pass `replacing`).

Fix: order the checks terminal-first. A terminal row raises `EffectTerminal` (F2) regardless of hash; a `launched` row adopts regardless of hash (the launch already happened); `EffectConflict` applies only to a live `reserved` row with a different hash.
S21: terminal effect + reserve with a different request_hash -> `EffectTerminal`, never `EffectConflict`.

## F5 - concurrent first-run migration resets version to 0 (CONFIRMED)

`_initialize` reads `sqlite_master`/`PRAGMA table_info` *before* `BEGIN IMMEDIATE`; two processes on a pre-kernel DB both see `has_version=False`, and P2's rebuild wipes P1's preserved counters.

Fix: acquire `BEGIN IMMEDIATE` first, then re-read schema state under the lock, then decide. P2 sees P1's completed rebuild and skips. Keep the rebuild idempotent.
S22: two threads run the migration body concurrently against a pre-kernel copy; a version bumped between them survives (final version >= 1, never reset to 0).

## F6 - wake delivery skips deliverability + parent-state guard (PLAUSIBLE)

`WakeDelivery.deliver` checks only that the parent row exists and has a holder; it never calls `ownership.assert_deliverable` (which `send_all` does) nor checks `parent.state`, so it can send into a retired/superseded session and consume the dedupe key, and can send to a terminal parent (violates "terminal sessions refuse machine mail").

Fix: before sending, call `ownership.assert_deliverable(parent.holder_session_id)` and require `parent.state` live; otherwise mark the wake `resolved` with a reason (not delivered). With F1's re-arm this is safe: a fresh cohort event re-arms.
S23: child completes while parent is retired/terminal -> wake `resolved` with reason, zero `client.send`, and a later live cohort event still wakes.

## F7 - guarded readback outside its transaction for activate/checkpoint (PLAUSIBLE)

`transition()` opens a no-BEGIN connection; with `isolation_level=None` the UPDATE and readback SELECT autocommit separately. `finish_with_wake`/`prepare_replacement` wrap their calls, but `activate()` and `checkpoint()` do not, so the returned record / `StaleTransition.current` can describe a row another writer moved.

Fix: wrap `activate()` and `checkpoint()` in `BEGIN IMMEDIATE` (pass the connection into `transition`, the parameter already exists).
S24: concurrent `activate` and a competing terminal transition -> the readback each caller sees matches the row its own UPDATE produced (no cross-writer misreport).

## Gates

- All of S1-S24 green; full suite green 3.11-3.13; simulator run 3x no flake.
- Re-prove migration idempotency AND the new F5 concurrency test against a copy of the real `escalations.sqlite3`.
- No new blind UPDATE; every guarded transition's readback shares its transaction.
