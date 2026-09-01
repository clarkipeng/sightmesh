# Kernel v1 review fixes (round 2)

Three HIGH defects from the second review, all introduced by round-1 fixes, plus one LOW.
Root: the new `cohort_signature`/`already` dedup treats historical or suppressed records as covering future genuine events; expiry treats an unknowable executor error as proof of absence.
Fixes are minimal and targeted. Each gets a locking scenario (S25-S27) red-before/green-after. F2/F4/F5/F7 were re-confirmed clean - do not touch them.

## G1 - resolved wake poisons re-arm (F1xF6 interaction, HIGH)

`wakes.py:146` `record_wakes` suppresses a new wake if ANY row matches `(dedupe_key, cohort_signature)`, including a `resolved` row. F6 resolves a wake whenever the parent is *transiently* undeliverable (quarantined mid-handoff). After the handoff completes, an unchanged cohort re-scanned by the reconciler matches the resolved row and never re-arms - the manager hangs.

Fix: the coverage check must count only rows that actually cover the event - live (`pending`, `claimed`) or `delivered`. A `resolved` row (suppressed, never delivered to the manager) must NOT suppress re-arm.
Change the `already` query to `... AND state IN ('pending','claimed','delivered')`.
S25: child cohort terminal -> wake created; force the parent transiently undeliverable so pump resolves it; restore deliverability; reconciler re-scans the unchanged cohort -> a new wake arms and delivers. (Red today: resolved row suppresses forever.)

## G2 - identical repeated cohort roster never re-wakes (HIGH)

`cohort_signature` fingerprints only child ids + state (`wakes.py:118-121`). A genuinely new transition into an identical satisfying state hashes the same and is suppressed by the earlier delivered wake. Case: `a1` blocks -> wake delivered -> manager `replace("a1")` -> `a1` blocks again (new reason) -> identical signature -> suppressed -> manager hangs. Same for a `lost` child replaced then `lost` again.

Fix: include the child's epoch (and version) in the signature so a replaced-then-repeated child is a distinct event. `replace` bumps child epoch, so `{(a1, epoch=1, blocked)}` != `{(a1, epoch=2, blocked)}` -> re-arms; while a reconciler re-scan of the unchanged roster keeps identical epochs -> correctly suppressed. Minimal: add child epoch/version to the per-child tuple the signature hashes; no new counter.
S26: `a1` blocks, delivered; `replace("a1")`; `a1` blocks again -> a second distinct wake arms and delivers. (Red today: identical signature suppresses.)

## G3 - transient executor failure orphans a live launch (F3 residual, HIGH)

`effects.py:241` `_native_effect` catches bare `CdesktopError` and returns `None` ("absent"), so a 500 / URLError / 15s timeout during the expiry sweep retires a reservation whose native session is actually alive - orphaning it. Absence of proof of life is not proof of death.

Fix: only a definitive not-found (404) may retire the reservation. An unknowable error (5xx, URLError, timeout) must leave the reservation intact for the next tick. The status detail is available - `_probe_managed_launch` already distinguishes via `"404" not in str(exc)` (`sdk.py:725`); use the same test (or the typed status on `CdesktopRejectedError`). On unknowable error, skip this candidate this tick, do not retire.
S27: expiry sweep hits an expired reservation with a live native session while the executor raises a 500/timeout -> reservation stays `reserved`, native session not orphaned; a subsequent tick with the executor reachable adopts it. (Red today: retired as lost.)

## G4 - one un-adoptable candidate aborts the expiry batch (LOW)

`expire_reservations` calls `store.activate(...)` in the adopt path; if the task moved out of `ACTIVATE_PREDECESSORS` (finished while the sweep ran), `StaleTransition` propagates and `reconcile_kernel` swallows it, skipping the rest of the batch this tick. Self-heals next tick.

Fix: isolate per candidate - wrap each candidate's adopt/retire in its own try/except (as `_retire_reservation` already tolerates a moved row), so one moved task never skips the others.

## Gates

- S25-S27 green; existing S1-S24 stay green; full suite green 3.11-3.13; simulator 3x no flake.
- No new blind coverage check; resolved wakes never suppress re-arm; only 404 retires a reservation.
- Note in the report whether `cohort_signature` should be replaced wholesale by a monotonic per-parent event counter if a third review finds more signature-class bugs - for now keep the minimal epoch-inclusion fix.
