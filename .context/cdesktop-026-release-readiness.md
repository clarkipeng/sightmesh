# cdesktop 0.2.6 Release Readiness

Date: 2026-08-18
Worktree: `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/6d94-cdesktop-026-rel/cdesktop`
Branch: `cdt/6d94-cdesktop-026-rel`

## Scope And Authority

- Required base: `origin/main` at `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`.
- Current HEAD during audit: `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`.
- Primary checkout was not touched.
- No release was published, no tag was moved, no PR was opened, and no commit was made.
- Final source tree is restored to the exact base. The only intended remaining worktree addition is this report.

## Tag And Source Proof

- Local tag: `v0.2.6-20260816210919`.
- `git show-ref --tags v0.2.6-20260816210919` returned annotated tag object `e25f45c196e17a988533f0f35c3ef2e8048c9da1`.
- `git cat-file -t v0.2.6-20260816210919` returned `tag`.
- `git rev-parse v0.2.6-20260816210919^{}` returned `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`.
- `git rev-parse origin/main` returned `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`.
- Therefore the local tag peels to the exact required base.

Relevant source commits present in that base:

- `7e657e1c fix: make execution stops replay-safe (#2)`.
- `8b70b761 feat: expose durable session command recovery (#3)`.
- `62cbae3d chore: bump version to 0.2.6`.

## Recovery Contract Evidence

The exact base contains the durable session-command recovery surface SightMesh expects:

- `crates/db/src/models/session_command.rs` persists `SessionCommand` rows with `pending`, `claimed`, `done`, `failed`, and `cancelled` states.
- It supports deduped enqueue, ordered claim, `release_execution`, `requeue_execution`, `requeue_killed_execution`, `finish_execution`, and durable command listing.
- `crates/server/src/routes/sessions/mod.rs` exposes:
  - `GET /sessions/{session_id}/commands`
  - `POST /sessions/{session_id}/commands/requeue`
  - `POST /sessions/{session_id}/commands/dispatch`
  - `defer_dispatch` on follow-up requests.
- `crates/services/src/services/container.rs` dispatches pending commands, claims them only after execution creation, releases orphaned claimed commands on startup cleanup, and finishes commands on execution completion/failure.
- `crates/db/src/models/execution_process_stop_operation.rs` and `crates/server/src/routes/execution_processes.rs` provide keyed stop replay outcomes: accepted, rejected, and interrupted.

## Checks Run

Focused recovery checks:

- `cargo test -p db session_command -- --nocapture`
  - Result: passed.
  - Evidence: 7 tests passed, 0 failed.
- `cargo test -p db execution_process_stop_operation -- --nocapture`
  - Result: passed.
  - Evidence: 7 tests passed, 0 failed.

Repository release/schema checks:

- Initial `pnpm run prepare-db:check` was run concurrently with other cargo commands and failed with a transient build-artifact error: `failed to map object file: memory map must have a non-zero length`.
- Serial rerun: `pnpm run prepare-db:check`
  - Result: passed.
  - Evidence: migrations applied, `SQLx check complete!`.
- `pnpm run generate-types:check`
  - Result: passed after the already-generated workspace state was restored for reporting.
  - Evidence recorded from the successful run: `shared/types.ts is up to date`.
- `pnpm run format`
  - First attempt failed because the isolated worktree did not yet have frontend dependencies installed (`prettier: command not found`).
  - After `pnpm install`, `pnpm run format` completed successfully.

Broad check:

- `pnpm run check` was started before the authority correction and exited with existing remote-web TypeScript errors in routine navigation types and `ExecutorConfigForm.tsx`.
- Per corrected authority, no further broad check was run and no source changes were retained for those unrelated errors.

External release-build evidence supplied by release coordination:

- All six platform release builds previously passed:
  - `linux-x64`
  - `linux-arm64`
  - `windows-x64`
  - `windows-arm64`
  - `macos-x64`
  - `macos-arm64`

## Release Workflow And Artifacts

- `.github/workflows/pre-release.yml`:
  - Bumps version, tags release candidates, builds frontend, builds backend binaries for six platforms, packages the npx CLI, uploads platform zips and manifest to R2, and creates a GitHub prerelease with the frontend zip and npm tarball.
- `.github/workflows/publish.yml`:
  - Runs on GitHub release `released` events or manual dispatch.
  - Downloads the `.tgz` release asset and publishes that exact package to npm with provenance.
- Version metadata at base is aligned for the local cdesktop release line:
  - root `package.json`: `0.2.6`
  - `npx-cli/package.json`: `0.2.6`
  - `packages/local-web/package.json`: `0.2.6`
  - workspace Rust crates in the local app line: `0.2.6`
  - `crates/tauri-app/tauri.conf.json`: `0.2.6`
- Remote-only packages remain on their own version line and are not part of this local 0.2.6 publish decision.

## Remaining Risks / Blockers

- There is no GitHub prerelease for `v0.2.6-20260816210919`.
- Workflow run `31972553761` skipped `create-prerelease` after the R2 upload failed, so there are no durable prerelease artifacts for an operator to convert or reuse.
- Release creation is blocked only by absent R2 repository secrets required by the prerelease workflow upload steps.
- Required repository secrets from `.github/workflows/pre-release.yml`:
  - `R2_BINARIES_ACCESS_KEY_ID`
  - `R2_BINARIES_SECRET_ACCESS_KEY`
  - `R2_BINARIES_ENDPOINT`
  - `R2_BINARIES_BUCKET`
  - `R2_BINARIES_PUBLIC_URL`
- Current repository secret inventory is empty.
- No source defect requiring a release-readiness PR remains in this worktree.
- `pnpm run check` currently reports unrelated remote-web route typing errors; these were not fixed because the corrected authority explicitly excluded further broad-check work and source changes.

## Exact Publish Action

After the required R2 repository secrets are configured and the operator explicitly approves release preparation:

1. Dispatch `.github/workflows/pre-release.yml` from current `main` with `version_type=none`.
2. That path reuses package version `0.2.6`, mints a fresh timestamped tag, rebuilds all release artifacts, uploads them to R2, and creates a new GitHub prerelease.
3. SightMesh must pin the newly created tag and tarball from that successful prerelease run.
4. Promoting that prerelease to a full GitHub release is a separate explicit operator decision because it triggers `.github/workflows/publish.yml` and publishes the attached `cdesktop-*.tgz` package to npm.

Do not push or move tags manually, do not merge, do not publish, and do not dispatch release workflows until the operator explicitly approves.
