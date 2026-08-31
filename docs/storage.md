# Local storage and retention

SightMesh keeps orchestration data local and separates durable source state from disposable execution state. It does not copy entire repositories or maintain a second source-of-truth transcript store.

| Owner | Default location | Contents | Retention |
| --- | --- | --- | --- |
| Git and the repository host | Repository and Git refs | Source, commits, branches, and review history | Explicit Git lifecycle |
| cdesktop | `~/Library/Application Support/ai.cdesktop.cdesktop` | Workspace records, visible session history, and process metadata | Until the archive is explicitly deleted |
| cdesktop | `~/.local/share/sightmesh/.cdesktop-workspaces` | SightMesh-managed isolated worktrees | Active lifetime; a clean archived worktree may be reclaimed after about one hour |
| Repowire | `~/.repowire` | Local peer identity, discovery, and request/reply state | Until explicitly reset |
| SightMesh | `~/.local/state/sightmesh` | Ownership leases, route policy, approval audit, logs, migration plans, and bounded handoffs | Lifecycle-specific; approval audit, migration plans, and handoffs remain until explicitly removed |
| SightMesh | `~/.local/share/sightmesh/updates` | Checksum-verified, versioned cdesktop update installations | Active and pending packages plus one recent spare; superseded packages are pruned automatically |
| SightMesh | `~/.config/sightmesh` | Provider profile identifiers and routing policy | Until explicitly changed or removed |
| Workspace owner | `<workspace>/.context` | Workspace-local notes and handoffs | Follows the workspace and remains Git-ignored |
| Conductor during migration | Existing Conductor database and workspaces | Original transcripts, workspace metadata, and checkouts | Preserved until post-migration reconciliation authorizes removal |

`sightmesh configure` and managed-service installation harden SightMesh state and configuration trees to owner-only access. They also make the top-level Repowire, cdesktop, and SightMesh worktree roots owner-only when those paths exist. `sightmesh doctor` fails if one of those top-level roots is accessible by group or other users. Child files owned by Repowire or cdesktop remain under their applications' control and are protected by the private parent directory.

## Workspace lifecycle

Archive is the ordinary retirement action. It stops execution, disables message routing, releases the ownership lease, and preserves the cdesktop workspace record and session history. SightMesh refuses to archive a dirty cdesktop-managed worktree because cdesktop may reclaim that directory after about one hour. A worktree cdesktop has already reclaimed holds no uncommitted work, so it never counts as dirty. A direct workspace may preserve reconciled dirty state only with `--preserve-dirty`; cdesktop never owns or removes its repository.

Restore reactivates the same cdesktop record, reacquires its ownership lease, and re-enables routing. If cdesktop already reclaimed a clean managed worktree, it recreates the worktree from the preserved Git branch when execution resumes.

Delete is deliberately separate from archive. It requires an archived workspace plus `--confirm-delete`, removes the cdesktop record, session history, process logs, and any cdesktop-owned worktree, and preserves the Git branch. Branch removal is a later explicit Git operation after reconciliation. Direct repositories are never deleted by this command.

## Migration data

`sightmesh migrate plan` is read-only. Applying a plan adopts existing Conductor checkouts as direct cdesktop workspaces and does not start a model. A bounded semantic handoff may be written under the private migration state directory so a resumed agent can recover immediate context. The original Conductor transcript database remains the complete authority until the user verifies the migrated workspace and explicitly retires the old data.

Do not place secrets in prompts, transcripts, `.context`, handoffs, command arguments, or the orchestration repository. Local-only storage and restrictive permissions reduce exposure but do not turn those surfaces into a secrets manager.

Approval decisions are stored in `~/.local/state/sightmesh/approvals.sqlite3`. The database and its WAL files are owner-only. Rejection text is not copied into this store; SightMesh records only its SHA-256 digest so an audit can correlate a decision without creating another transcript.

Parent return addresses and queued commands live in cdesktop's application database. During the v0.9 update, legacy SightMesh delivery and relationship databases are imported, copied to `~/.local/state/sightmesh/legacy`, and removed from the active state directory.

Task handoff may carry only the opaque content-addressed history reference from
the [cdesktop task launch contract](cdesktop-task-launch-v1.md). cdesktop and
the executor enforce transcript, fork, and free-disk limits before writing;
SightMesh neither stores transcript bytes nor claims it can prevent writer-side
disk growth.
