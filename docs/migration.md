# Conductor to cdesktop migration dry-run

Run:

```sh
python scripts/migration-dry-run.py --json
```

The command is strictly read-only. It inventories Conductor roots, workspace checkouts, Git branches, HEADs, dirty paths, worktree lists, safe metadata file names, and SQLite table counts opened with SQLite `mode=ro`. It skips Git internals, dependency caches, generated output, prompt/transcript/message-like metadata names, and workspace contents when looking for Conductor metadata databases. It does not write Conductor SQLite, stop workers, create cdesktop workspaces, modify source repositories, or read credential-like file contents.

The output proposes a cdesktop workspace name for each discovered Conductor workspace and lists blockers. Current blockers are dirty Git state and non-Git workspace directories. Treat any dirty paths as migration stop signs until the owning worker has checkpointed or committed them.

The dry-run is an input to a human-reviewed migration plan. It is not an execution engine and intentionally has no mutation flag.
