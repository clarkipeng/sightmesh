# Subscription hot-swap integration order

All steps below remain proposals only. They require explicit operator approval where noted; this document authorizes no merge, ready transition, workflow dispatch, release, secret mutation, or runtime-lock update.

## SightMesh train

1. PR #17 lease isolation: `e4f90f8c4745db911105b3b318f0a94d3aea16d0` onto current `main`.
2. PR #18 Lane C settings/selector: `e3cab92cee77a4dbc2dde0fe522d518d6b9986da`, rebased onto the new `main`.
3. PR #19 C2 safety: `be40617b0d232cfa02d11a59b3192b00a1591f11`, kept directly above rebased #18.
4. PR #23 Lane D succession/routed auto-launch: `fdf12e0c6552d2dafd54b4e3893f6dd6a70b3ea2`, rebased above C2.
5. Escalation side stack must travel with D before release composition: PR #22 K `fa2defe148e06e3e2f6ba4df45dc3b5b7b973f0d`, then PR #24 L `8bd82e7c14c4b358da0cb1dfaa34417082500ae4`. Rebase this stack onto the post-D tree, preserving L above K.
6. PR #20 Lane H docs: `a94b8e3e760b86f0063bdd8db03abda79dd92b03`, rebased after the implementation and escalation stacks so its integration-boundary wording remains truthful.
7. Rebase experimental composition PR #16 `4fec36b0f1e4073a0b9e350ecc060d63c67d7095` last. Compare its resulting tree with both the previously reviewed head and the new base; retain only release-polish/runtime-lock composition intended for the new train.

Conflict notes:

- #18/#19/#23 are a deliberate C → C2 → D stack around routing selection and reconciler consumption; do not flatten them out of order.
- K and D overlap in `src/sightmesh/cli.py`, `tests/conftest.py`, and `tests/test_cli.py`. Resolve semantically: preserve K's durable launcher/escalation wiring and D's routed spawn/quarantine behavior. L stays above K and its intent/ack semantics must survive.
- #20 currently descends from Lane C, not D/K/L; rebase it after those stacks and update any claims that overstate outcome-read/UI coverage.
- #16 is composition-only and last because an early rebase could revert implementation or pin an unpublished runtime.

## cdesktop train

1. PR #5 frontend/format baseline: `41d37b261ada0d03b73e82cfd59d1fa39140a61b`.
2. PR #8 atomic rejected-spawn proof: `6defea82b0436970f382ab7191679a8cafc55628`.
3. PR #9 failed-workspace-start cleanup: `0ca04288e5cd988bf3a3776923715702ac87bd6d`.
4. A0 backend baseline: `5d2f132ff147a08f6879488eab2d6556e5a90dd3`.
5. PR #7 A1 attempts: `c2a9c2eaacfdd4b2dea066c95793faf755b834be`, kept above A0.
6. PR #10 B auth/outcomes/approval backend: `96960fbe4ab1ecc7feea22d6bc9b1ab7eee03a34`, kept above A1.
7. PR #6 E Agents/approval UI: `fa9600cf34c67d89ff82287f76f1cd6cd35116ed`, rebased onto the resulting #5 + A0/A1/B tree.
8. PR #4 release distribution: `398668b54ff5f725575f660cc0bca62a240996af`, rebased last onto the complete cdesktop tree. Compare the candidate tree against its new base to prove it does not revert any implementation.

Conflict notes:

- #8 owns teammate validation only; #9 owns failed workspace-start cleanup only. Preserve their focused regressions while rebasing them sequentially.
- A0 → A1 → B is a contract stack. `execution_process.rs`, launch metadata, dispatcher/outcome plumbing, generated types, and migrations must retain that order.
- E currently descends from #5 rather than B. Its three-file integration delta is frontend-only, but the rebase must validate its locally declared approval shapes against B's generated types. Do not expose `auth_binding_id` or invent normalized-outcome data; B still lacks an outcome read route.
- #4 touches release workflows/distribution and is last. Re-run its release-contract checks only after the final implementation tree is fixed.

## Evidence gates before readiness

- Independently re-establish B's worker-reported 163/0 focused evidence when resources permit.
- Install/restore the defined frontend toolchain and run E's TypeScript, formatting, and web checks; its stacked PR currently receives no CI because the workflow accepts only `main`-based pull requests.
- Add/review a backend normalized-outcome read surface before claiming E outcome display coverage.
- Preserve D 217/0, K focused 55/0, L 204/0, and I 1/0 exact-head evidence through rebases; rerun affected checks after conflict resolution.
- Keep every PR draft through the complete exact-head review.

## Operator decisions and held actions

1. F-8: decide whether to restack PR #5 onto A0, waive its four structurally expected backend failures, or accept the red stacked baseline. The manager must not choose.
2. Approve or decline the cdesktop `version_type=none` prerelease workflow dispatch after PR #4 is rebased and validated.
3. After verifying real release assets, explicitly approve one SightMesh runtime-lock update with the actual tag, URL, SHA-256, and compatibility floors.
4. Decide whether and when the SightMesh experimental release composition may be marked ready, merged, or published.

Until those decisions and evidence gates close, stop at clean pushed draft heads.
