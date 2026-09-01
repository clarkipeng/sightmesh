# Storage Reclamation - scoped spec

Status: plan for the one remaining lifecycle gap.
Evidence: 435 workspaces / ~82GB accumulated in one week (manually reclaimed during a disk-full incident); archived workspaces retain full worktrees including node_modules indefinitely; `archive_workspace` marks the row and stops dev servers but deletes nothing on disk.

## Problem statement

Auto-archive (shipped) fixes bookkeeping but not disk. An archived workspace's directory persists forever, so heavy fleet use still grows disk usage without bound. The missing piece is the afterlife: reclamation of archived workspaces' directories.

## Existing primitives this builds on (do not create new ones)

1. The reconciler sweep - already runs periodically; auto-archive and signal policies ride it today. Reclamation is one more step in the same sweep.
2. `workspaces.worktree_deleted` - the db already has the flag; nothing sets it after archive. Reclamation sets it.
3. `Workspace::worktree_path()` - resolves the on-disk path; reclamation uses it to know what to delete.
4. The dirty-file doctrine - existing archive guard semantics: existing directory with uncommitted changes is protected; missing directory is reconciled by definition.

## The fix (one lane, cdesktop + zero sightmesh changes expected)

Add a reclamation step to the reconciler sweep:

1. Select workspaces where `archived = true AND worktree_deleted = false`.
2. If `archived_at` is older than the retention period (default 7 days, same settings surface as auto-archive):
   - Resolve the worktree path. If it does not exist, set `worktree_deleted = true` and stop - nothing to reclaim.
   - Run the existing dirty check on the directory. Dirty: skip, increment a skip counter, leave a visible flag on the workspace row (reuse the existing flag surfaces; no new machinery). Clean: delete the directory, set `worktree_deleted = true`.
3. Deletion is of the workspace directory only. The db row is never deleted - history, transcripts, and outcomes remain queryable. This matches the existing doctrine that the record is the durable artifact and the checkout is derived state.

## Explicit non-goals

- No hard disk-cap eviction valve. If reclamation works, the cap is dead code; if the soak shows reclamation failing, revisit with evidence.
- No deletion of db rows, transcripts, or outcomes.
- No new daemons, config surfaces, or cron entries.

## Invariant

After this ships, disk usage from workspaces is bounded by: live workspaces + archived-but-dirty workspaces + one retention window of clean archived workspaces. Nothing else can accumulate.

## Tests

1. Clean archived workspace past retention: directory deleted, flag set, row intact.
2. Dirty archived workspace past retention: directory preserved, flag unset, visible flag recorded.
3. Archived workspace with missing directory: flag set, no error.
4. Archived workspace inside retention window: untouched.
5. Running workspace: never selected.
6. Sweep idempotence: running it twice back-to-back is a no-op the second time.

## Live gate

On the real fleet: after the lane lands and activates, the 7 currently archived-but-present workspaces (4.2GB) get reclaimed by the sweep without manual action, and `du` on the workspaces directory drops accordingly. Evidence recorded in the PR.
