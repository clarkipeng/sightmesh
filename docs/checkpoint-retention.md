# Checkpoint retention and retries

On a native server advertising execution-evidence version 1, ordinary
`SightMesh.checkpoint` publishes the checkpoint's exact bytes as a native artifact
occurrence. The task store owns its operation identity and final reference; the
retention client has no separate database or copied evidence directory.

```python
from sightmesh.sdk import CheckpointPending

try:
    worker = mesh.checkpoint(text, worker="audit")
except CheckpointPending as pending:
    # Keep the working copy. Retry this operation, not a new checkpoint.
    worker = mesh.checkpoint(text, worker="audit", operation_id=pending.operation_id)
```

The existing CLI accepts the same identity with
`sightmesh checkpoint 'checkpoint text' --worker audit --operation-id UUID`.
A normal call without an ID is a **new logical checkpoint**, even for equal text.
Retries use the saved ID, native execution, original name/path, and byte facts.
Pending identities remain in `task_checkpoint_operations` after a process crash;
`TaskStore.pending_checkpoint_operations(task_id)` reads them without replaying an
effect. Do not infer retry identity from a content digest.

The SDK resolves the task holder's actual native coding-agent execution, then
persists the operation under the task fence before publication. Native HTTP runs
outside that fence. The final native receipt, checkpoint transition, and task
history commit together only if the expected task version, epoch, and holder still
match. A completed task cannot be revived by a late receipt.

Receipt checks include native occurrence/attachment IDs, execution, publication
key, task/epoch producer reference, original path/name, capture time, hash, size,
and confirmed native durability. Equal-byte occurrences retain distinct links.
Recovery verifies a surviving working copy; if it is missing or corrupt, it
verifies and reads the native occurrence. A reclaimed worktree path is not needed
to recover a committed native reference.

## Two separate durability acknowledgements

A native receipt is not a durable task reference. The actual task-store writing
connection must use WAL with synchronous FULL (or stronger) and fullfsync enabled.
Only a transaction written under that policy gets a reference-commit marker. After
COMMIT, the directory entries for the actual SQLite filename and configured path,
including ancestor and intermediate symlink entries, must be confirmed before the
SDK acknowledges success. SQLite owns its file descriptors; the extra barriers
open only directories, never another database-file descriptor.

This follows SQLite's [WAL and synchronous contract](https://www.sqlite.org/pragma.html#pragma_synchronous)
and [macOS fullfsync behavior](https://www.sqlite.org/pragma.html#pragma_fullfsync).
It is a supported filesystem/VFS acknowledgement, not a hardware power-loss test.
External directory renames must not race confirmation. Unsupported or failed
barriers keep the working copy and return an uncertain acknowledgement. A failed
acknowledgement may follow a committed reference; retry verifies it without
creating another occurrence or repeating the task transition.

No checkpoint call deletes a working copy, installs a runtime, or migrates live
evidence. Older native versions retain the existing **local-only** checkpoint
behavior and do not acknowledge a retained occurrence. An advertised but failing
evidence contract never silently downgrades to retained success. Runtime activation
and the incident-hybrid task accounting migration require their separate gates.
