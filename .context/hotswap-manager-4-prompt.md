# Authority

You are the sole visible manager for the subscription hot-swap tonight train. Use visible SightMesh workers only. Do not implement feature code. Do not use hidden/native subagents.

# Mandatory harness rule

Never launch `sleep`, detached/background commands, `nohup`, shell `&`, or polling processes. Do not use a long-running terminal command as a monitor. Use short foreground `sightmesh peers`, `sightmesh peek`, Git reads, queued messages, and worker completion callbacks only. Two prior managers were killed immediately after creating a detached polling shell.

# Read

- `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-tonight-plan.md`
- `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-implementation-plan.md`
- `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/5509-lane-f-adversari/sightmesh/.context/lane-f-adversarial-review.md`

# Exact current state

- Prior manager sessions are non-authoritative. Do not resume them. You are the only manager.
- SightMesh PR #17 is draft at `e4f90f8c4745db911105b3b318f0a94d3aea16d0`; focused lease tests passed, remote checks last seen 4 pass, 2 pending, none failed.
- SightMesh PR #18 is draft and green 6/6 at base `e3cab92cee77a4dbc2dde0fe522d518d6b9986da`.
- C2 security/validation corrections are clean and pushed at `be40617b0d232cfa02d11a59b3192b00a1591f11` on `cdt/1ebb-lane-c2-routing`, stacked on PR #18. Focused routing tests: 22 passed. It fixes trace redaction, non-secret selection including metered ask, and truthful route validation. Open a draft stacked PR if none exists.
- H docs are clean and pushed at `4e5de2d95b971808b7b24711142a0a20156bf10c` on `cdt/a9cc-lane-h-routing-d`, based on PR #18. It is docs-only and explicitly marks B/D integration as pending. Open a draft stacked PR if none exists.
- cdesktop A0 is isolated commit `5d2f132ff147a08f6879488eab2d6556e5a90dd3` on base `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`.
- Sole A1 writer `@lane-a-contract-2` is active on `cdt/1879-lane-a-contract`. It has five modified Rust files and is running focused tests. Duplicate `@lane-a-outcome-c2` was killed and must not resume.
- Sole E writer `@lane-e-dashboard-shell` is active on `cdt/964b-lane-e-dashboard`, based on cdesktop PR #5 exact head `41d37b261ada0d03b73e82cfd59d1fa39140a61b`.
- cdesktop PR #4 remains draft at `398668b54ff5f725575f660cc0bca62a240996af`. SightMesh PR #16 remains draft at `4fec36b0f1e4073a0b9e350ecc060d63c67d7095`.

# Queue

1. Independently inspect C2 and H diffs and open draft stacked PRs. Do not merge or mark ready.
2. Monitor A1 with short foreground checks. Require focused tests, clean commit, push, draft stacked PR, exact SHA, and compact consumer contract.
3. Once A1 is stable, immediately launch B on its exact head. B owns cdesktop auth-binding resolution, redaction, normalized live adapter outcomes, and durable metered `auto`/`ask`/`never` approval/resume. Secrets resolve only immediately before launch.
4. Launch D from C2 exact head plus A1's frozen consumer fixture. D owns SightMesh autolaunch/reconciler, cooldown/requeue, restart recovery, and cross-executor successor linkage.
5. Review E exact head and adapt only after B API fixture stabilizes. E must not edit backend contracts.
6. Launch a final read-only rereviewer against exact A1/B/C2/D/E heads for concurrency, restart, approval exactly-once, and secret leakage.

# Guardrails and stop

One writer per hotspot. All PRs draft. No merge, ready, publish, workflow dispatch, secret mutation, or runtime lock update. Continue until implementation lanes have clean pushed draft heads and exact-head evidence, or a genuine operator choice is required. Report compact milestones to the launcher with `sightmesh parent --message`.
