# Lane A1 — outcome contract successor

Start from exact cdesktop base `5d2f132ff147a08f6879488eab2d6556e5a90dd3` only. This is the distinct A0 SQLx/clippy baseline and must remain a separate commit. Do not repeat repository-wide discovery and do not inspect/apply the predecessor's WIP stash (`lane-a-wip`): it is untested and unwired.

Own only the outcome/attempt contract seams identified in the tonight plan: normalized process outcome, logical command + attempt linkage, opaque auth-binding reference, and exact-once stale-attempt protections. Primary hotspots: `crates/db/src/models/execution_process.rs`, cdesktop executor/container dispatch and normalized outcome mapping, and tightly focused Rust tests. Do not touch provider/auth secret resolution (Lane B), app navigation/settings UI (Lane E), formatter/frontend baseline, release distribution, or unrelated schema work.

Product invariants: one logical command has at most one active attempt; completion of any attempt prevents stale later launch; unknown state fails closed; durable records contain opaque auth-binding IDs only, never secrets/paths/headers; use existing native command/execution records rather than a new table unless reconstruction demonstrably fails.

First action is a narrow read-only inventory of the specified seams and relevant focused tests. Then implement one coherent A1 contract checkpoint, run the directly relevant Rust/API tests, commit and push it, open a draft stacked PR, and report exact head, files, tests, and stable interface fixture to `sightmesh parent --message`. Stop after the first stable contract commit; do not start B/D work.
