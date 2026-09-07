# Failure accounting cutover

`attempts` counts failed epochs, not launches or checkpoints. A fresh store
records `failure_accounting=1` in `evidence_contract`. Startup refuses existing
unversioned task tables and unknown versions before changing task projections.
Automatic legacy task-table rebuilds have been removed.

An operator-approved reset starts a new budget, not a reconstruction of past
failures. `failure_accounting.reset_budget(path, expected_fingerprint=...)`
accepts only the current task schema in an existing WAL database. It preserves
every task field except `attempts` (zero) and `version` (incremented once).
It does not resurrect terminal tasks or retain a separate legacy counter copy.
Existing history stays intact; a store without history begins with the new
budget as an observed baseline, explicitly marked as missing past history.

Before running it:

1. Verify the candidate build, simulator and independent adversarial review.
2. Stop and verify **all old writers**, including services and agent commands.
   Keep them stopped through activation. A SQLite transaction or task version
   cannot fence an old binary that resumes afterward.
3. Make a consistent SQLite online backup. Review that exact snapshot and approve
   the fresh budget for every selected task. Compute its fingerprint inside a
   read transaction using the same candidate build. Never use `immutable=1` on
   a live WAL database; it is only for complete offline copies.
4. Call `reset_budget` on the explicit target path with that fingerprint. Under
   `BEGIN IMMEDIATE`, it checks all schema and rows, resets budgets, records
   history and confirms the contract atomically. Any drift refuses the reset.
5. Verify read-back and durability, install the verified runtime pair, then run
   a bounded canary with only new writers before releasing the writer barrier.

A pre-commit error rolls everything back. A post-commit durability error means
keep writers stopped and recover forward. Retrying a confirmed version returns
`False` without resetting any newly charged failures. Never restore a stale
whole-database backup over newer state or restart old writers after cutover.

Older task schemas are unsupported, not automatically repaired or discarded.
