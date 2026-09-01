# Authority

Perform an independent, read-only release-blocker review of the combined cdesktop and SightMesh runtime dependency changes. Do not edit either source tree.

# Exact candidates

- SightMesh: `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/0c5b-release-candidat/sightmesh`, base `6e0bf793c982c251a6c489a65d5edc57c11ce7f4`, candidate `4fec36b0f1e4073a0b9e350ecc060d63c67d7095`, draft PR #16.
- cdesktop: `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/6d94-cdesktop-026-rel/cdesktop`, base `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`, candidate `3d9ef0c7`, branch `cdt/6d94-cdesktop-026-rel`.

Verify both exact heads before reviewing. If the cdesktop owner has not pushed `3d9ef0c7` yet, review that exact clean local commit rather than an older remote head.

# Contract to audit

1. cdesktop remains a separate public repository, not a submodule or vendored source tree.
2. A cdesktop prerelease succeeds without R2 credentials and publishes one flat deterministic set: 18 platform/binary ZIPs, `manifest.json`, and one npm tarball.
3. One asset inventory is authoritative. Workflow matrices and tests cannot silently drift from it.
4. The packaged CLI is pinned to one exact GitHub release directory and authenticates the binary manifest before parsing it. The SightMesh-pinned npm package SHA-256 must transitively pin the downloaded binary bytes. A mirror override may change location only, not accepted content.
5. Cached and fresh binaries are checked against authenticated size and SHA-256 data. Corrupt manifest, missing asset, wrong size, and wrong digest fail closed.
6. The public local prerelease path has no active R2 assumption. Unrelated remote/Tauri distribution is not accidentally broken or claimed as covered.
7. SightMesh owns one packaged strict runtime lock for cdesktop repository, released version/tag, exact package URL/SHA-256, general minimum, and durable-recovery minimum. Bootstrap, updater defaults, CLI version checks, durable feature detection, docs, package smoke, and compatibility CI derive from it.
8. The current lock remains on the real 0.2.5 GitHub asset and byte digest `eeacc90f8f91bfa7bf6c5415a0b3cb6484ad5040055cda06c82e61a6490d7fdf`. No nonexistent 0.2.6 asset is pinned.
9. Package/path overrides are explicit and fail closed. Remote overrides cannot silently bypass checksum verification.
10. Both pull requests remain drafts. No release, workflow dispatch, merge, ready transition, secret mutation, or primary-checkout mutation is allowed.

# Evidence

Inspect both full diffs and owning instructions. Run focused, non-destructive checks that can falsify the contract, including the cdesktop release-contract test and package build/type surface, SightMesh runtime-lock tests and real pinned-artifact probe, wheel resource inspection, shell/YAML checks, and stale-pin searches. Inspect workflow ordering closely enough to prove the manifest digest is computed before npm pack and the manifest is not later changed in a hash cycle. Distinguish a source-shape edge check from real binary compatibility.

Do not repeat broad checks when exact-head CI or local evidence is already sufficient. Do not write source, commit, push, comment on PRs, or create another PR.

# Report

Write `.context/runtime-dependency-independent-review.md` in your own SightMesh review worktree. Lead with release blockers, each with severity, exact file/line, reproduction, and smallest root fix. Then list verified invariants, exact commands/results, head SHAs, CI state, and whether each candidate is safe to proceed to the explicit publish decision. If there are no blockers, say so unambiguously.

Stop after the report and a concise parent status. Do not fix findings yourself.
