# Operations gates

## Fleet status and idle prompting

Task surfaces answer from the kernel store and never call cdesktop. Native inventory is a separate command, so a routine status check cannot fan out over every workspace and session.

`sightmesh status` shows managed service state, managed task rows and counts, staged/active update versions, ownership leases, and named profiles.
`sightmesh list` lists managed tasks across every scope; `sightmesh tasks` lists the caller's scope.
`sightmesh attention` is the single queue of work needing a human: blocked approvals, tripped breakers, lost tasks, and unacknowledged deliveries. Dirty closeouts and failing checks are cdesktop-owned facts; when no caller supplies them the queue reports them as `reported-degraded` rather than implying a clean fleet.
`sightmesh workspaces` is the explicit native inventory (workspaces, sessions, providers, profiles) and fans out on purpose. Add `--include-archived` for history and `--overview` for the native execution projection.

Use `sightmesh peers` for the compact agent-facing roster and `sightmesh peek @AGENT` for a coalesced current-activity snapshot.

`sightmesh overview` groups managed tasks into **Needs attention**, **Running**, and **Done since view**, with a stable selector, one reason, and one safe next action. The default view bound is the last 24 hours; pass `--since ISO_TIME` to choose another boundary. The bound is derived at read time and adds no view-state persistence.

`sightmesh --json workspaces --overview` uses the fixed privacy-safe fleet projection and does not copy transcripts. Model comes from the native execution action. Its selected cdesktop provider ID is used only to join the native provider kind; it is not reported as a SightMesh subscription account. Token usage, context limit, and derived context pressure appear only when cdesktop supplies numeric values in a normalized `token_usage_info` entry for the selected process, with that provenance recorded. Account, quota, monetary cost, and any other unavailable fact remain `null`; SightMesh does not infer them.

Use `sightmesh prompt-idle @AGENT --message-file FILE` for automation that must not append work behind an active turn or bypass a pending approval. It reads that session's process state immediately before sending and fails closed unless the target is active and idle.

`sightmesh message @AGENT` always appends a native `continue` command; cdesktop starts it only at that session's next safe boundary. `steer` is the explicit `replace` path.

Use `sightmesh inbox` for one global view of pending questions, plans, and tool requests. A manager agent can copy the included response templates into one `sightmesh respond --responses JSON` call. The complete batch is structurally validated before any response is sent. Live timeout races are isolated per response, so one expired request does not prevent later valid responses from being attempted.

Use `sightmesh steer @AGENT --message-file FILE` when the current turn must change immediately. SightMesh resolves an exact ambiguity-safe selector, refuses self-steering and pending interactions, stops only that session's active non-dev-server execution, verifies the persisted state left `running`, and sends a native follow-up. Peer sessions in the same worktree and the dev server remain running. The visible transcript and the session's executor, model, reasoning, permissions, and provider remain intact.

Workspace display names can be corrected without changing Git state:

```sh
sightmesh workspace rename WORKSPACE_ID repository/task-name
```

cdesktop remains the viewing surface. Use its repository-grouped sidebar, conversation panel, process logs, changed-file tree and diff, Open in IDE action for the complete checkout, and built-in preview browser for running applications or image review.

## Staged cdesktop updates

With no arguments, staging uses the exact released package, version, and SHA-256 in the [runtime lock](../src/sightmesh/runtime-lock.json):

```sh
sightmesh update stage
sightmesh update status
sightmesh update prune --dry-run
sightmesh update cancel
```

An override requires explicit verification: `sightmesh update stage --package URL_OR_LOCAL_TGZ --version VERSION --sha256 SHA256`. An unverified local archive is accepted only with the explicit `--local-development` flag; remote packages always require a digest. Staging downloads and verifies the archive, installs
it under `~/.local/share/sightmesh/updates`, runs the staged executable's version check,
checks the platform-specific backend ZIP and every member without executing the raw
backend, and records pending state atomically under `~/.local/state/sightmesh/update.json`. It
does not overwrite the globally installed cdesktop package or restart a worker.

Run `sightmesh update activate` when you want to apply the staged release. Activation
drains first: it asks cdesktop to refuse new launches, then waits a bounded window for
in-flight turns to reach terminal. cdesktop caps one drain request at 30 seconds, so the
wait renews the refusal on a shorter cadence and stops as soon as a renewal fails,
reporting `rearms` and `lapsed` alongside the time it actually waited. A loaded host
converges because no new work is admitted behind the drain. If the window expires the
drain is released and the result reports `drain-timed-out` with the activity actually
observed. The bridge stays up for the whole wait and is stopped only for the swap
itself, so a refused activation costs no bridge downtime at all. Dev servers do not
block activation because they are disposable children of the backend.

Activation checks the new backend health endpoint and exact reported version before
restarting the bridge. Failure is recorded, clears pending activation, and is never
retried automatically. SightMesh does not keep or restore an update rollback plist.
The updater never claims to preserve an active executor stream across a backend
restart. CLI and skill updates are one-shot and can take effect while cdesktop workers
continue.

The one supported bootstrap exception is `0.2.3-sightmesh.1`, which predates the drain
endpoint. Its transition to `.2` still waits the bounded window before restart, keeps
the bridge up throughout, and reports `enforced: false` for the drain it could not
request. An
unknown backend that lacks the drain endpoint fails closed.

`sightmesh doctor` reports `cdesktop-version-skew`, comparing the installed CLI, the
running service, and the active release, and failing when they disagree or stale
releases remain past the prune budget.

## Plan approvals

Inspect and decide pending visible-agent plans without taking over the worker transcript:

```sh
sightmesh approval list
sightmesh approval show APPROVAL_ID
sightmesh approval approve APPROVAL_ID
sightmesh approval reject APPROVAL_ID --reason "Narrow the write scope and resubmit"
sightmesh approval history --limit 20
```

The response is bound to both the approval ID and its execution-process ID. A mismatched process is rejected without consuming the pending approval. When the command runs inside cdesktop, `CDESKTOP_SESSION_ID` identifies the reviewer, self-approval is forbidden, and only the earliest session in that workspace is the lead. Outside cdesktop, the local macOS user is recorded as the human reviewer.

`ExitPlanMode` is the normal plan approval. Questions may be answered in cdesktop or through `sightmesh respond`; the global inbox derives their structured choices from the same normalized conversation. Other tool requests fail closed unless a batch item contains `"allow_non_plan": true`. cdesktop currently changes an approved Claude plan session from plan mode to its bypass-permissions mode, so inspect the plan and its worktree boundary before approving it.

Approval attempts are written to the private local audit store before cdesktop is called. The audit includes the decision, reviewer, target IDs, status, and a SHA-256 digest of a rejection reason. It deliberately does not duplicate the reason or transcript text.

For short automation inputs, use `--prompt`, `--message`, `--checkpoint`, or `--reason`. Their `*-file` forms remain available for multiline or reusable content, and the two forms are mutually exclusive.

## Provider profiles and failover

Configure provider credentials in cdesktop through its supported provider UI. SightMesh stores only a provider UUID and non-secret defaults:

```sh
sightmesh --json profile providers
sightmesh profile set codex-work-api \
  --executor CODEX \
  --provider CDESKTOP_PROVIDER_UUID \
  --credential-kind api \
  --automatic-failover
```

`credential-kind=ambient` records a normal CLI login or consumer subscription; `api` and `enterprise` record a keyed provider. Any kind may opt into `--automatic-failover` when the operator configured that provider through its supported interface.

Profiles are explicit `spawn` and `failover` destinations. Independently, a credential pool orders operator-owned accounts for the Claude or Codex CLI. `sightmesh pool exec` takes the first account with quota and moves to the next account when one is exhausted. Selection never prints, extracts, or replays credentials, or pushes requests past a reported limit.

On a capacity or authentication boundary, create a durable checkpoint and run:

```sh
sightmesh failover WORKSPACE_ID \
  --profile codex-work-api \
  --checkpoint-file handoff.md
```

The default starts a visible successor session in the same cdesktop workspace. This preserves dirty files and the human-visible transcript while changing the explicitly selected provider. `--new-worktree` requires a clean source and starts a separate workspace. The source remains active unless archival is explicitly confirmed.

## Workspace ownership leases

Use `sightmesh lease acquire --owner <name> --repo <path>` before taking recovery or migration ownership of a repository. Add `--worktree <path>` when the active checkout is a worktree. Leases are local JSON records under `~/.local/state/sightmesh/leases`, written under an interprocess `fcntl.flock`.

Lease liveness is TTL-based, not CLI-PID-based. One-shot `sightmesh lease acquire` leases remain live until they expire or are explicitly renewed or released. Use `sightmesh lease renew <token>` to extend ownership and `sightmesh lease release <token>` when ownership is handed off. The bridge reconciles active cdesktop workspaces every two seconds, renews known leases, and backfills leases for active pre-SightMesh workspaces.

Conflict rules preserve safe worktree parallelism:

- a direct-checkout lease conflicts with every live lease for the same repository;
- a worktree lease conflicts with a direct-checkout lease for the same repository;
- two worktree leases for the same repository can coexist when their canonical worktree paths differ;
- two worktree leases conflict when their canonical worktree paths match.

Expired leases are recoverable with `sightmesh lease recover-stale`; acquisition also prunes stale records before deciding conflicts. Corrupt lease state fails closed until the invalid file is inspected and repaired or removed.

`sightmesh spawn` is lease-gated. Direct checkout spawn acquires before starting the workspace and attaches the returned workspace/session IDs afterward. Worktree spawn first refuses when a direct lease controls the repository, then acquires the specific returned worktree path after cdesktop reports the container. `sightmesh workspace archive WORKSPACE_ID --confirm-reconciled` releases only the archived workspace's persisted lease token after successful archive.

Archive, restore, and delete use cdesktop's native workspace lifecycle. Archive stops the workspace, hides it from the active list, and preserves its database history. cdesktop reclaims a clean archived managed worktree after about one hour; execution after restore recreates it from the preserved Git branch. SightMesh therefore refuses to archive any dirty managed worktree, even with `--preserve-dirty`. That exception is limited to direct workspaces because cdesktop never owns or removes their repositories.

`sightmesh workspace restore WORKSPACE_ID` makes an archive active, reacquires its ownership lease, and re-enables its Repowire bridge route. It rolls the workspace back to archived if lease synchronization fails. `sightmesh workspace delete WORKSPACE_ID --confirm-delete` requires an already archived workspace. It deletes the cdesktop record, transcript, process logs, and owned managed worktree while deliberately preserving the Git branch. Dirty managed worktrees are always refused. A dirty direct repository is never deleted and requires `--preserve-dirty` before only its cdesktop history is removed. A repository path that has already disappeared is reconciled by definition, so archive and delete both proceed and record it under `missing_repositories`; only directories that still exist can hold dirty state. Branch deletion remains an explicit native Git operation after reconciliation.

No command ever prints a capability token. `lease renew` and `lease release` return the public lease representation, because the caller already holds the token it passed in. `lease acquire` is the single deliberate capability exit: it writes the token to a 0600 file under the lease directory's `capabilities/` and emits only `capability_path`. The CLI's one output path rejects any credential-shaped field, so a future leak fails loudly instead of serializing into a transcript. Workspace-to-token mappings are stored separately under the lease state directory so archival can release only the owning workspace.

## Recovery smoke boundary

`scripts/recovery-smoke.sh` defaults to dry-run mode. With `DRY_RUN=0`, it creates a temporary `HOME`, fake disposable `cdesktop` and `sightmesh` executables, installs SightMesh LaunchAgent plists without starting launchd, and verifies lease workspace release inside that temporary state root. It never stops Conductor workers, provider sessions, cdesktop sessions, or unmanaged launchd labels.

Managed service operations remain scoped to `io.sightmesh.cdesktop` and `io.sightmesh.bridge`. Installation removes the obsolete updater label. Cutover touches the former two legacy labels only and saves their plist definitions for rollback.

## Install ownership

`scripts/install-local.sh` never displaces a path it does not own. A skill link belonging to another product makes install refuse with that path named; remove it with its own uninstaller first. Because nothing is ever displaced, uninstall is a pure delete of created paths and has nothing to restore.

Install records what it created in `~/.local/state/sightmesh/install-manifest.json` (0600): the `uv tool` entry, the `~/.claude/skills` and `~/.codex/skills` links, and the state roots (`~/.local/share/sightmesh`, `~/.local/state/sightmesh`, the `io.sightmesh.*` LaunchAgent plists).

`scripts/uninstall-local.sh` removes only ownership-verified links, plists, and the `uv tool` install, then removes the manifest. Durable state under `~/.local/share/sightmesh` and `~/.local/state/sightmesh` is kept on purpose; delete it explicitly to finish.
