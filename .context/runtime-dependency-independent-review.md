# Independent runtime-dependency release-blocker review

Date: 2026-08-18 (America/Los_Angeles)  
Rereviewed cdesktop head: `398668b54ff5f725575f660cc0bca62a240996af` on draft PR #4

## Release blockers

**No remaining release blockers were found at the rereviewed exact heads.**

### RESOLVED — cached CLI path now always uses the authenticated archive provider

- Candidate: cdesktop `398668b54ff5f725575f660cc0bca62a240996af`.
- Location: `npx-cli/src/cli.ts:126-170`; regression: `scripts/test-release-contract.mjs:206-261`.
- Result: `extractAndRun()` unconditionally awaits its archive provider at `cli.ts:151`, with `ensureBinary` as the default. The old caller-side `existsSync(zipPath)` shortcut is gone. `ensureBinary()` remains the single production owner of authenticated-manifest lookup and cached/fresh size/SHA-256 validation.
- Reproduction:

  ```sh
  pnpm run check:npx-cli
  pnpm run test:release-contract
  ```

  Result: both passed. The launcher regression creates a corrupt preexisting cache ZIP, injects a provider returning a different validated ZIP, proves the provider was called, proves the validated archive was extracted/launched, and proves the corrupt cached file was not consumed or overwritten.

No SightMesh release blocker was found in the runtime-lock changes.

## Verified invariants

- Both candidate trees were clean before review and matched the exact requested heads:
  - SightMesh base `6e0bf793c982c251a6c489a65d5edc57c11ce7f4`, candidate `4fec36b0f1e4073a0b9e350ecc060d63c67d7095`.
  - cdesktop base `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`, candidate `398668b54ff5f725575f660cc0bca62a240996af` (the exact clean pushed commit was rereviewed).
- The full diffs and repository/root `AGENTS.md` files were inspected. SightMesh does not add cdesktop as a submodule or vendored tree; the projects remain separate repositories.
- `scripts/github-release-assets.mjs:7-25` owns the six-platform/three-binary inventory and deterministic flat naming. It requires and emits exactly 18 ZIPs plus `manifest.json`. `scripts/test-release-contract.mjs:180-215` compares both workflow matrices to that inventory, preventing silent matrix drift.
- `.github/workflows/pre-release.yml:404-476` has no active R2 dependency in the local prerelease path. It prepares the 18 flat ZIPs and manifest, hashes the final manifest bytes before `npm pack`, injects that digest and the exact tag release directory into the bundled CLI, then only adds the npm tarball. No later step mutates the manifest, so there is no hash cycle. The release upload is `release-assets/*` and the layout gate requires 18 ZIPs, one manifest, and one tarball.
- `.github/workflows/pre-release.yml:519-538` copies the repository root `LICENSE` into `npx-cli/LICENSE` after bundle/contract injection and before `npm pack`.
- The contract test fails closed when required workflow markers disappear. In isolated negative probes, deleting the LICENSE-copy line failed at the explicit `licenseCopyIndex != -1` assertion; replacing `__BINARY_MANIFEST_SHA256__` failed the explicit marker regex. The test cannot pass merely because relative ordering indexes both evaluate to `-1`.
- `.github/workflows/test.yml:7-11` selects PRs changing ordinary files, `pre-release.yml`, or `test.yml`; `:122-129` selects `npx-cli/**`, both release scripts, both relevant workflows, and package/lock inputs; `:173-192` runs `check:npx-cli` and `test:release-contract`. A Test workflow run was created by PR #4 for exact head `398668b…`, proving workflow-level PR selection. At report time that newest run (`32214089532`) is queued/pending, so job completion is not claimed.
- The public CLI release base is one exact GitHub release directory. `CDESKTOP_RELEASE_ASSET_BASE_URL` changes location only; the compiled manifest digest remains fixed. The manifest is hashed before JSON parsing (`npx-cli/src/download.ts:103-123`). Missing assets and fresh-download size/digest mismatches fail closed. The launcher now routes existing ZIPs through `ensureBinary()` so cached size/digest validation cannot be bypassed.
- The prerelease change does not claim Tauri coverage. `npx-cli/src/cli.ts:238-247` explicitly rejects `--desktop` before the dormant Tauri downloader is reached. The unrelated `.github/workflows/publish.yml` retains its prior R2 flow and was not changed by this candidate.
- SightMesh has one strict packaged lock at `src/sightmesh/runtime-lock.json`. It records repository `clarkipeng/cdesktop`, version `0.2.5`, tag `v0.2.5-20260813115508`, the real GitHub tarball URL, SHA-256 `eeacc90f8f91bfa7bf6c5415a0b3cb6484ad5040055cda06c82e61a6490d7fdf`, general minimum `0.2.5`, and durable-recovery minimum `0.2.6`.
- Lock parsing is exact-key and fail-closed. Bootstrap, update-stage defaults, doctor/version checks, durable feature detection, docs, wheel/package smoke, and compatibility workflow derive from the lock. Searches found no stale duplicated tag, package URL, or digest and no pin to a nonexistent 0.2.6 asset.
- Bootstrap requires a digest for package overrides unless explicitly marked local development. Update staging requires SHA-256 for remote overrides; an unverified override is limited to the explicit local-development path.
- Compatibility CI distinguishes the real pinned artifact check from the advisory source/package-shape edge check; the latter is not represented as binary compatibility.
- Both relevant PRs remain drafts: SightMesh PR #16 and cdesktop PR #4. No workflow was dispatched and no release, merge, ready transition, secret mutation, comment, push, or candidate-tree change was performed.

## Commands and results

- `git rev-parse HEAD; git status --porcelain=v1` in both exact worktrees: requested SHAs; clean.
- `git diff --stat`, `git diff --name-status`, full targeted diffs, and `git diff --check` for both base-to-candidate ranges: inspected; diff checks passed.
- `node scripts/test-release-contract.mjs`: **passed** (`release contract tests passed`), including the launcher cache regression.
- Isolated cdesktop `npm run build`: **passed**; bundled `bin/cli.js` produced.
- `pnpm run check:npx-cli`: **passed** at `398668b…`.
- `pnpm run test:release-contract`: **passed** at `398668b…`, including the corrupt-preexisting-cache launcher regression.
- Isolated negative contract probes: **passed**; removing either the pre-pack LICENSE copy or manifest-digest injection marker made the suite fail.
- `sh -n scripts/bootstrap-local.sh`; `bash -n scripts/check-cdesktop-runtime.sh scripts/package-smoke.sh`: **passed**.
- Ruby YAML parse of both changed workflows: **passed**.
- Focused isolated SightMesh runtime-lock/CLI/durable tests: **11 passed, 58 deselected**. A broader selected test invocation also exposed four unrelated existing test-double failures in other PR-composition code; exact-head PR CI is green, and those failures do not exercise this runtime-lock diff.
- `./scripts/check-cdesktop-runtime.sh`: **passed**; downloaded the real pinned 0.2.5 package, verified the exact digest, installed without scripts, and got the expected `cdesktop/0.2.5 ...` version surface.
- Isolated wheel build and ZIP inspection: **passed**; exactly one `sightmesh/runtime-lock.json` resource was present.
- Isolated `scripts/package-smoke.sh`: **passed**, including wheel install, CLI surfaces, runtime-lock import, and Twine checks.
- Stale-pin/R2/submodule searches: only the unchanged remote publish workflow retained R2; no stale SightMesh runtime identity outside the lock and no cdesktop submodule/vendor entry were found.

## CI and proceed decision

- SightMesh draft PR #16: exact head `4fec36b0f1e4073a0b9e350ecc060d63c67d7095`; GitHub reports **10 passed, 0 failed**. The SightMesh candidate is safe to proceed to an explicit publish decision on its runtime-lock evidence.
- cdesktop draft PR #4: pushed head `398668b54ff5f725575f660cc0bca62a240996af`; the prior blocker is fixed and locally regression-tested. GitHub has created exact-head Test run `32214089532`, currently queued/pending. The PR summary still reports no completed checks because that run has not attached/completed yet.
- Combined decision: **no code release blocker remains. The candidates are safe to proceed to the explicit publish decision after the selected exact-head cdesktop PR CI run completes successfully. Do not publish while that run is pending.**
