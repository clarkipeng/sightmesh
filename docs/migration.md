# Conductor migration

SightMesh migrates Conductor workspaces by adopting their existing directories in place. It does not copy Git worktrees, delete source data, stop Conductor sessions, or start model requests during import.

## Create a plan

```sh
sightmesh --json migrate plan --conductor-root ~/conductor
```

The command reads Conductor SQLite in read-only mode and writes a private `plan.json` under `~/.local/state/sightmesh/migrations`. The plan joins Conductor records, session status, Git branch and HEAD, dirty paths, retained worktrees, and archived context directories. It also includes filesystem worktrees or archives that no longer have a matching database record.

The compatibility command remains available:

```sh
sightmesh --json migration-dry-run
```

It is a lightweight inventory only. Use `sightmesh migrate plan` for an executable, resumable plan.

## Review gates

Before apply:

1. Pause every selected Conductor session. Apply rechecks session state and refuses working, running, starting, or compacting sessions.
2. Review every dirty checkout. Dirty state is preserved in place but requires both `--include-dirty` and `--confirm-checkpointed`.
3. Keep the Conductor database and source directories until the migrated workflow has been accepted.

The plan is a point-in-time record. Apply refuses a checkout whose HEAD or dirty-path inventory changed after planning.

## Apply

Migrate current active workspaces:

```sh
sightmesh migrate apply PLAN_PATH \
  --all \
  --confirm-conductor-paused
```

Migrate one named workspace:

```sh
sightmesh migrate apply PLAN_PATH \
  --workspace nara \
  --include-archived \
  --include-dirty \
  --confirm-conductor-paused \
  --confirm-checkpointed
```

Add `--include-archived` to catalog archived Conductor records, retained orphaned worktrees, and context-only archives as private handoffs. Cataloged archives do not create cdesktop workspace rows, so historical records do not flood its sidebar. Add `--materialize-archived` only for archives that must be browsable as cdesktop rows. The command is resumable. Completed entries in `run.json` are skipped on later invocations.

For each selection, SightMesh:

- exports a bounded semantic handoff from the original local transcript while retaining the Conductor database as the complete authority;
- records the original context directory rather than duplicating it;
- uses the private transcript handoff directory when an archived checkout and context directory are no longer present;
- adds a private `.context/sightmesh-migration.json` pointer only when `.context` is Git-ignored;
- creates a worktree-disabled cdesktop workspace for each active selection, using its original concise name, and attaches the existing directory without starting an agent;
- reuses an existing cdesktop workspace attached to the exact same path;
- leases active imported checkouts;
- keeps source-archived selections catalog-only unless materialization is explicit, then immediately archives their cdesktop rows.

No source branch, file, worktree, transcript, or archived context is deleted.

The convenience script creates a plan and prints the guarded apply command:

```sh
./scripts/migrate-conductor.sh --conductor-root ~/conductor
```

## Status and rollback

```sh
sightmesh migrate status PLAN_PATH
sightmesh migrate rollback PLAN_PATH --confirm
```

Rollback archives only cdesktop workspaces created by that run and releases their SightMesh leases. It refuses a workspace after a cdesktop session has been created because that workspace may contain unique new history. Reused preexisting workspaces and private context bundles are never removed.

## Starting migrated work

Open cdesktop with `sightmesh service open`, select the migrated workspace, and create a Claude Code or Codex session. Before writing, read `.context/sightmesh-migration.json` when present and then read the referenced handoff. Context-only archived workspaces open their archived directory directly.

Because the imported workspace uses the original checkout, do not resume the corresponding Conductor session after cutover. Keep one visible owner and one active tool per checkout.

## Execution-routing upgrade note

After migration, keep launching and supervising work in cdesktop. The execution-routing settings introduced with the subscription hot-swap train are separate, local policy and do not migrate provider credentials or alter existing cdesktop sessions. Configure routes only after the operator-owned account pool is healthy, then use `sightmesh routing validate` and `sightmesh routing explain` to inspect the selector without launching work. The current selector does not yet execute a selected route or recover/approve a route swap in cdesktop; retain the existing cdesktop workflow until that integration lands.

`sightmesh pool serve` remains a loopback recovery/compatibility view for account-pool diagnostics. It is not the primary session UI and should not be used as an alternative to cdesktop for active work.
