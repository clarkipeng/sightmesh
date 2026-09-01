# Authority

Fix only the independently reproduced cdesktop main formatting baseline in a separate visible worktree and draft PR.

# Base

Fetch and verify exact cdesktop `origin/main` SHA `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`. Read and follow cdesktop `AGENTS.md`. The primary checkout is dirty and must remain untouched.

# Reproduced failures

- `cargo fmt --all -- --check` and the remote-manifest equivalent report only `crates/server/src/routes/routines.rs`.
- `pnpm --filter @vibe/web-core run format:check` reports only:
  - `packages/web-core/src/shared/components/BranchChip.tsx`
  - `packages/web-core/src/shared/components/ComposerChipRow.tsx`
  - `packages/web-core/src/shared/components/FolderChip.tsx`
  - `packages/web-core/src/shared/components/routines/RoutineDetailContent.tsx`
  - `packages/web-core/src/shared/components/routines/RoutinesListContent.tsx`
- `pnpm --filter @vibe/ui run format:check` reports only:
  - `packages/ui/src/components/CreateChatBox.tsx`
  - `packages/ui/src/components/Navbar.tsx`
  - `packages/ui/src/components/WorkspacesSidebar.tsx`
- local-web and remote-web format checks pass.

# Required work

1. Apply only the repository formatters' mechanical output to those nine files. Do not change behavior.
2. Fix the owning root format command so future `pnpm run format` also runs the existing UI formatter. Keep the script change minimal and consistent with the current package scripts.
3. Prove `cargo fmt --all -- --check`, remote Cargo format check, web-core format check, UI format check, local-web format check, and remote-web format check pass. Run only lightweight syntax/type checks selected by any non-mechanical script edit.
4. Verify the diff is exactly the nine mechanical files plus the root format-script owner, with no generated files or unrelated cleanup.
5. Commit, push, and open a draft PR against `main` with `~/.local/bin/gh-axi`. Keep it draft.

# Exclusions

Do not edit the cdesktop release-distribution branch or PR #4, change generated files, publish, dispatch workflows, mark ready, merge, touch secrets, or modify the primary checkout.

# Stop condition

Report the exact head, draft PR URL, changed paths, and checks. Stop with a clean pushed worktree.
