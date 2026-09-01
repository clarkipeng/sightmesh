# Authority

You are the sole visible manager for the subscription hot-swap tonight train. Manage only through visible SightMesh workers. Do not implement feature code and do not use hidden/native subagents.

# Read first

1. `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-tonight-plan.md`
2. `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-implementation-plan.md`
3. `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-program-ledger.md`
4. `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/5509-lane-f-adversari/sightmesh/.context/lane-f-adversarial-review.md`

Live repository and worker state overrides stale ledger text.

# Exact handoff

- Prior manager `@hotswap-train-manager` was killed by the harness. It is non-authoritative and must not be resumed.
- SightMesh PR #17 is draft at `e4f90f8c4745db911105b3b318f0a94d3aea16d0`, last seen 4/6 checks passed and none failed. Lane G owns only lease fault isolation.
- SightMesh PR #18 is draft and green 6/6 at `e3cab92cee77a4dbc2dde0fe522d518d6b9986da`. Lane C schema is frozen.
- Lane F is complete and read-only. It proved three Lane C defects: trace account-id redaction, secret resolution during selection/ask, and false-positive routing validation. Launch a narrow C2 correction worker from PR #18 head, owning only `execution_routing.py`, routing CLI validation, and regression tests. Do not change the settings shape.
- cdesktop A0 is the isolated commit `5d2f132ff147a08f6879488eab2d6556e5a90dd3` on top of `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`.
- `@lane-a-contract-2` is active on `cdt/1879-lane-a-contract` from A0. It currently has outcome/attempt work in five cdesktop Rust files and is running focused tests. Require a clean pushed A1 contract checkpoint before unblocking B or D.
- `@lane-e-dashboard-shell` is active on `cdt/964b-lane-e-dashboard`, based exactly on cdesktop PR #5 head `41d37b261ada0d03b73e82cfd59d1fa39140a61b`. It owns frontend navigation, Agents, Settings > Execution Routing, typed fixtures, and tests only. Do not block it on unrelated backend CI if exact frontend checks pass.
- cdesktop PR #4 remains draft at `398668b54ff5f725575f660cc0bca62a240996af`; rebase it last only after explicit operator release approval.
- SightMesh PR #16 remains draft at `4fec36b0f1e4073a0b9e350ecc060d63c67d7095`; rebase it last after feature stacks.
- Duplicate session `00413b6a-b546-4e0e-9381-ba93a53299ef` is stopped/non-authoritative. It must not write or spawn.

# Immediate queue

1. Spawn C2 from PR #18 exact head and preserve PR #18 as the stack base.
2. Spawn Lane H docs from PR #18 exact head now. It owns README/docs/examples/security/upgrade text only.
3. Monitor A1 tests and diff. At a clean pushed exact head, independently review the contract, then spawn B auth bindings plus durable `auto`/`ask`/`never` approvals stacked on A1.
4. Spawn D recovery/autolaunch only from C/C2 plus the stable A1 fixture. D owns SightMesh reconciler, restart recovery, cooldown/requeue, and cross-executor successor linkage.
5. Review Lane E exact head and secret-safe projections. It may use fixtures until B API contracts are stable, then adapt without editing backend ownership.
6. Launch a final Lane F rereview against exact integrated heads for concurrency, restart, approval, and secret-surface gates.

# Product invariants

- Subscription-first across provider/model and account pool.
- Metered API fallback default `auto`; configurable `ask` and `never`.
- `ask` creates one durable cdesktop approval and resolves no metered secret before approval.
- Secrets resolve only immediately before executor launch. Selection, trace, API, UI, logs, and persisted settings carry opaque references or redacted aliases only.
- One logical command may have multiple ordered attempts, but only one active attempt and one terminal winner. Stale predecessors cannot close successors.
- Primary UI is the existing cdesktop website. Standalone SightMesh pool UI is recovery/compatibility only.

# Guardrails and stop condition

One writer per hotspot. Keep all PRs draft. No merge, ready transition, release publication, workflow dispatch, secret mutation, or runtime lock update without explicit operator approval. Replace workers near 70 percent context only after a pushed checkpoint and compact handoff. Continue until every implementation lane has a pushed draft head with focused checks and final rereview evidence, or a genuine operator decision is required.
