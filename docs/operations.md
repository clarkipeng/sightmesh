# Operations gates

## Fleet status and idle prompting

`sightmesh status` joins managed service state, active cdesktop workspaces, latest process state, pending approvals, unseen turns, Repowire route policy, delivery counts, ownership leases, redacted cdesktop providers, and named SightMesh profiles. Add `--include-archived` only when historical workspaces are relevant.

Use `sightmesh prompt-idle SESSION_ID --message-file FILE` for automation that must not append work behind an active turn or bypass a pending approval. It reads cdesktop's active workspace summary immediately before sending and fails closed unless the target is active and idle.

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

`credential-kind=ambient` is appropriate for a normal CLI login or consumer subscription, but SightMesh rejects `--automatic-failover` for that kind. API and enterprise profiles may opt in.

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

`sightmesh spawn` is lease-gated. Direct checkout spawn acquires before starting the workspace and attaches the returned workspace/session IDs afterward. Worktree spawn first refuses when a direct lease controls the repository, then acquires the specific returned worktree path after cdesktop reports the container. `sightmesh close --archive --confirm-reconciled` releases only the archived workspace's persisted lease token after successful archive.

Use `sightmesh --json lease list` for inspection. Workspace-to-token mappings are stored separately under the lease state directory so archival can release only the owning workspace.

## Recovery smoke boundary

`scripts/recovery-smoke.sh` defaults to dry-run mode. With `DRY_RUN=0`, it creates a temporary `HOME`, fake disposable `cdesktop` and `sightmesh` executables, installs SightMesh LaunchAgent plists without starting launchd, and verifies lease workspace release inside that temporary state root. It never stops Conductor workers, provider sessions, cdesktop sessions, or unmanaged launchd labels.

Managed service operations remain scoped to the explicit SightMesh labels `io.sightmesh.cdesktop` and `io.sightmesh.bridge`. Cutover touches the former two owned labels only and saves their plist definitions for rollback.
