# Lane F — read-only adversarial review

You are the independent, read-only adversarial reviewer for the subscription hot-swap release train. Do not edit implementation files, do not commit, do not open/update PRs, and own no source paths.

Read these plans first:
- `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-tonight-plan.md`
- `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-implementation-plan.md`

Stable evidence to inspect:
- SightMesh base `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e`.
- Lane G draft PR #17: `e4f90f8c4745db911105b3b318f0a94d3aea16d0`; focused claim `pytest tests/test_leases.py -q` = 16 passed; GitHub checks currently 4/6 passed, none failed.
- Lane C checkpoint: `e3cab92cee77a4dbc2dde0fe522d518d6b9986da`, pending draft PR and manager exact-head review.
- cdesktop baseline A0: `5d2f132ff147a08f6879488eab2d6556e5a90dd3`, distinct SQLx/clippy-only commit on base `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`.
- cdesktop frontend baseline draft PR #5: `41d37b261ada0d03b73e82cfd59d1fa39140a61b`; its remote checks currently report 3 passed/4 failed, so record that as a release risk, not an implementation task.

Objective: identify concrete, testable risks in exact-once command ownership, stale-attempt handling, restart/reconciliation, secret leakage, and metered `auto`/`ask`/`never` behavior. Review existing tests and stable diffs. Separate proven defects, insufficient proof, and non-issues. Do not speculate beyond code evidence.

Deliver a compact report at `.context/lane-f-adversarial-review.md` containing exact SHAs, files/lines, reproduction or missing proof, severity, and recommended owning lane. Send the report summary with `sightmesh parent --message`. Stop after the report; do not mutate source.
